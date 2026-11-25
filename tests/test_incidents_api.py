import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.incidents.dependencies import get_alert_store
from app.incidents.service import open_incident
from app.main import app
from app.models.monitor import Monitor
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import get_channel_cipher
from app.services.notifications.sql_store import SqlDeliveryStore
from app.util.config import get_settings
from app.util.database import get_db_session
from app.util.redis import get_redis_client
from tests.conftest import dispose_test_engine, make_test_engine
from tests.helpers.tenant import assert_tenant_isolated

sent_emails: list[tuple[str, str, dict[str, str]]] = []
CIPHER = ChannelCipher([Fernet.generate_key().decode()])
# In the past: API actions (acknowledge, notes, resolve) use the real clock, and
# real incidents never start in the future
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)


class FakeRedis:
    async def eval(self, *_: object) -> int:
        return 1


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def env(request, tmp_path, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setenv("JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("APP_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()
    sent_emails.clear()

    async def fake_send_email(kind: str, *, recipient: str, parameters: dict[str, str]) -> None:
        sent_emails.append((kind, recipient, parameters))

    monkeypatch.setattr("app.auth.service.send_email", fake_send_email)
    monkeypatch.setattr("app.organizations.service.send_email", fake_send_email)

    engine = await make_test_engine(request.param, tmp_path)  # every table
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = SqlDeliveryStore(engine)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: FakeRedis()
    app.dependency_overrides[get_channel_cipher] = lambda: CIPHER
    app.dependency_overrides[get_alert_store] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "sessions": sessions, "store": store}
    app.dependency_overrides.clear()
    await dispose_test_engine(engine)
    get_settings.cache_clear()
    get_redis_client.cache_clear()


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_account(client: AsyncClient, email: str) -> dict[str, str]:
    password = "StrongPassword123!"
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    _, _, params = next(m for m in reversed(sent_emails) if m[1] == email.lower() and m[0] == "otp_code")
    await client.post("/v1/auth/verify-email", json={"email": email, "code": params["otp_code"]})
    return (await client.post("/v1/auth/login", json={"email": email, "password": password})).json()["data"]


async def create_org(client: AsyncClient, tokens: dict[str, str], slug: str) -> str:
    resp = await client.post("/v1/organizations", json={"name": slug.title(), "slug": slug}, headers=bearer(tokens))
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


async def invite(client, org_id, owner, email, role, tokens) -> None:
    await client.post(
        f"/v1/organizations/{org_id}/invitations", json={"email": email, "role": role}, headers=bearer(owner)
    )
    msg = next(m for m in reversed(sent_emails) if m[0] == "invite" and m[1] == email)
    token = msg[2]["invite_url"].split("#token=")[1]
    await client.post("/v1/invitations/accept", json={"token": token}, headers=bearer(tokens))


async def create_monitor(client, org_id, tokens, name="Customer API", channel_ids=()) -> str:
    resp = await client.post(
        "/v1/monitors",
        json={
            "organization_id": org_id,
            "name": name,
            "type": "http",
            "target": "https://api.example.com/health",
            "notification_channel_ids": list(channel_ids),
        },
        headers=bearer(tokens),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


@pytest_asyncio.fixture
async def world(env) -> dict:
    client = env["client"]
    owner = await create_account(client, f"owner_{uuid4().hex[:6]}@example.com")
    editor_email, viewer_email = f"editor_{uuid4().hex[:6]}@example.com", f"viewer_{uuid4().hex[:6]}@example.com"
    editor = await create_account(client, editor_email)
    viewer = await create_account(client, viewer_email)
    outsider = await create_account(client, f"out_{uuid4().hex[:6]}@example.com")
    org_id = await create_org(client, owner, f"inc-{uuid4().hex[:6]}")
    await invite(client, org_id, owner, editor_email, "editor", editor)
    await invite(client, org_id, owner, viewer_email, "viewer", viewer)
    channel = await client.post(
        "/v1/notification-channels",
        json={"organization_id": org_id, "name": "On-call", "type": "email", "config": {"recipients": ["oncall@acme.com"]}},
        headers=bearer(editor),
    )
    channel_id = channel.json()["data"]["id"]
    monitor_id = await create_monitor(client, org_id, editor, channel_ids=[channel_id])
    other_org = await create_org(client, outsider, f"other-{uuid4().hex[:6]}")
    return {
        **env,
        "org_id": org_id,
        "other_org": other_org,
        "owner": owner,
        "editor": editor,
        "viewer": viewer,
        "outsider": outsider,
        "monitor_id": monitor_id,
        "channel_id": channel_id,
    }


async def open_on(world, monitor_id: str | None = None, started_minutes: int = 0) -> str:
    async with world["sessions"]() as s:
        monitor = await s.get(Monitor, UUID(monitor_id or world["monitor_id"]))
        incident, _ = await open_incident(
            s, monitor, started_at=T0 + timedelta(minutes=started_minutes), cause="HTTP 503 from API", now=T0
        )
        return str(incident.id)


def url(incident_id: str, action: str = "") -> str:
    return f"/v1/incidents/{incident_id}" + (f"/{action}" if action else "")


# --- list ------------------------------------------------------------------------------


async def test_any_member_can_list_newest_first(world):
    client = world["client"]
    second_monitor = await create_monitor(client, world["org_id"], world["editor"], name="Website")
    older = await open_on(world, started_minutes=0)
    newer = await open_on(world, second_monitor, started_minutes=5)

    resp = await client.get(f"/v1/incidents?organization_id={world['org_id']}", headers=bearer(world["viewer"]))
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()["data"]] == [newer, older]
    first = resp.json()["data"][0]
    assert first["status"] == "open"
    assert first["cause"] == "HTTP 503 from API"
    assert first["title"] == "Website is down"


async def test_filters_by_status_monitor_and_limit(world):
    client, editor = world["client"], world["editor"]
    second_monitor = await create_monitor(client, world["org_id"], editor, name="Website")
    first = await open_on(world)
    second = await open_on(world, second_monitor, started_minutes=5)
    await client.post(url(first, "resolve"), headers=bearer(editor))
    base = f"/v1/incidents?organization_id={world['org_id']}"

    async def ids(query: str) -> list[str]:
        resp = await client.get(base + query, headers=bearer(editor))
        assert resp.status_code == 200, resp.text
        return [i["id"] for i in resp.json()["data"]]

    assert await ids("&status=resolved") == [first]
    assert await ids("&status=open") == [second]
    assert await ids("&status=active") == [second]
    assert await ids(f"&monitor_id={world['monitor_id']}") == [first]
    assert await ids("&limit=1") == [second]


@pytest.mark.parametrize("query", ["&status=exploded", "&limit=0", "&limit=1000"])
async def test_bad_list_filters_are_rejected(world, query):
    resp = await world["client"].get(
        f"/v1/incidents?organization_id={world['org_id']}{query}", headers=bearer(world["viewer"])
    )
    assert resp.status_code == 422


async def test_requires_login(world):
    resp = await world["client"].get(f"/v1/incidents?organization_id={world['org_id']}")
    assert resp.status_code == 401


# --- get ---------------------------------------------------------------------------------


async def test_get_includes_the_timeline(world):
    client = world["client"]
    incident_id = await open_on(world)
    await client.post(url(incident_id, "acknowledge"), headers=bearer(world["editor"]))
    await client.post(url(incident_id, "notes"), json={"note": "DB failover"}, headers=bearer(world["editor"]))

    resp = await client.get(url(incident_id), headers=bearer(world["viewer"]))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "acknowledged"
    assert [e["type"] for e in data["timeline"]] == ["opened", "acknowledged", "note"]
    assert data["timeline"][2]["message"] == "DB failover"


async def test_unknown_incident_is_404(world):
    resp = await world["client"].get(url(str(uuid4())), headers=bearer(world["viewer"]))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "incident_not_found"


# --- tenant isolation -----------------------------------------------------------------------


async def test_other_organizations_cannot_see_or_touch_incidents(world):
    incident_id = await open_on(world)
    outsider = bearer(world["outsider"])
    client = world["client"]
    for method, path, body in [
        ("GET", url(incident_id), None),
        ("POST", url(incident_id, "acknowledge"), None),
        ("POST", url(incident_id, "resolve"), None),
        ("POST", url(incident_id, "notes"), {"note": "mine"}),
        ("GET", f"/v1/incidents?organization_id={world['org_id']}", None),
    ]:
        await assert_tenant_isolated(client, method, path, outsider_headers=outsider, json=body)

    # Same answer as a missing incident: no way to tell it exists
    resp = await client.get(url(incident_id), headers=outsider)
    assert resp.json()["error"]["code"] == "incident_not_found"


# --- acknowledge ------------------------------------------------------------------------------


async def test_editor_acknowledges(world):
    incident_id = await open_on(world)
    resp = await world["client"].post(url(incident_id, "acknowledge"), headers=bearer(world["editor"]))
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "acknowledged"
    assert resp.json()["data"]["acknowledged_at"] is not None


async def test_viewer_cannot_change_incidents(world):
    incident_id = await open_on(world)
    client, viewer = world["client"], bearer(world["viewer"])
    assert (await client.post(url(incident_id, "acknowledge"), headers=viewer)).status_code == 403
    assert (await client.post(url(incident_id, "resolve"), headers=viewer)).status_code == 403
    assert (await client.post(url(incident_id, "notes"), json={"note": "x"}, headers=viewer)).status_code == 403


async def test_acknowledging_twice_or_after_resolve_is_a_conflict(world):
    incident_id = await open_on(world)
    client, editor = world["client"], bearer(world["editor"])
    await client.post(url(incident_id, "acknowledge"), headers=editor)
    again = await client.post(url(incident_id, "acknowledge"), headers=editor)
    assert again.status_code == 409
    await client.post(url(incident_id, "resolve"), headers=editor)
    assert (await client.post(url(incident_id, "acknowledge"), headers=editor)).status_code == 409


# --- resolve ------------------------------------------------------------------------------------


async def test_resolve_with_a_note_and_resolved_alerts_are_queued(world):
    incident_id = await open_on(world)
    resp = await world["client"].post(
        url(incident_id, "resolve"), json={"note": "Restarted the database"}, headers=bearer(world["editor"])
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "resolved"
    assert resp.json()["data"]["resolved_at"] is not None

    detail = (await world["client"].get(url(incident_id), headers=bearer(world["viewer"]))).json()["data"]
    assert detail["timeline"][-1]["type"] == "resolved"
    assert detail["timeline"][-1]["message"] == "Restarted the database"

    queued = await world["store"].list_due(now=datetime.now(UTC) + timedelta(days=1))
    assert [(r.channel_id, r.payload["event"]) for r in queued] == [(world["channel_id"], "resolved")]


async def test_resolve_without_a_body_works(world):
    incident_id = await open_on(world)
    resp = await world["client"].post(url(incident_id, "resolve"), headers=bearer(world["editor"]))
    assert resp.status_code == 200


async def test_resolving_twice_is_a_conflict(world):
    incident_id = await open_on(world)
    client, editor = world["client"], bearer(world["editor"])
    await client.post(url(incident_id, "resolve"), headers=editor)
    assert (await client.post(url(incident_id, "resolve"), headers=editor)).status_code == 409


async def test_a_failing_alert_queue_does_not_fail_the_resolve(world):
    class BrokenStore:
        async def create_if_absent(self, **kwargs):
            raise RuntimeError("database blip")

    app.dependency_overrides[get_alert_store] = lambda: BrokenStore()
    incident_id = await open_on(world)
    resp = await world["client"].post(url(incident_id, "resolve"), headers=bearer(world["editor"]))
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "resolved"


# --- notes ----------------------------------------------------------------------------------------


async def test_add_note(world):
    incident_id = await open_on(world)
    resp = await world["client"].post(
        url(incident_id, "notes"), json={"note": "  Looking into it  "}, headers=bearer(world["editor"])
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["type"] == "note"
    assert resp.json()["data"]["message"] == "Looking into it"


@pytest.mark.parametrize("body", [{"note": ""}, {"note": "   "}, {"note": "x" * 5001}, {}, {"note": "x", "extra": 1}])
async def test_bad_notes_are_rejected(world, body):
    incident_id = await open_on(world)
    resp = await world["client"].post(url(incident_id, "notes"), json=body, headers=bearer(world["editor"]))
    assert resp.status_code == 422


async def test_notes_are_allowed_after_resolving(world):
    incident_id = await open_on(world)
    client, editor = world["client"], bearer(world["editor"])
    await client.post(url(incident_id, "resolve"), headers=editor)
    resp = await client.post(url(incident_id, "notes"), json={"note": "Post-mortem"}, headers=editor)
    assert resp.status_code == 201


# --- API keys --------------------------------------------------------------------------------------


async def make_api_key(world, scopes: list[str]) -> dict[str, str]:
    resp = await world["client"].post(
        "/v1/api-keys",
        json={"organization_id": world["org_id"], "name": f"key-{secrets.token_hex(3)}", "scopes": scopes},
        headers=bearer(world["owner"]),
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['key']}"}


async def test_api_keys_need_incident_scopes(world):
    incident_id = await open_on(world)
    client = world["client"]
    list_url = f"/v1/incidents?organization_id={world['org_id']}"

    monitors_key = await make_api_key(world, ["monitors:read"])
    denied = await client.get(list_url, headers=monitors_key)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "insufficient_scope"

    read_key = await make_api_key(world, ["incidents:read"])
    assert (await client.get(list_url, headers=read_key)).status_code == 200
    assert (await client.get(url(incident_id), headers=read_key)).status_code == 200
    assert (await client.post(url(incident_id, "acknowledge"), headers=read_key)).status_code == 403

    write_key = await make_api_key(world, ["incidents:write"])
    assert (await client.post(url(incident_id, "acknowledge"), headers=write_key)).status_code == 200
