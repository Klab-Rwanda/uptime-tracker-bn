import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.main import app
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import get_channel_cipher
from app.util.config import get_settings
from app.util.database import get_db_session
from app.util.redis import get_redis_client
from tests.conftest import dispose_test_engine, make_test_engine
from tests.test_status_pages_api import (
    FakeRedis,
    bearer,
    create_account,
    create_monitor,
    invite,
    sent_emails,
)

URL = "/v1/audit-logs"
SLACK_URL = "https://hooks.slack.com/services/T0001/B0002/abcdefSECRET123"


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def env(request, tmp_path, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setenv("JWT_SECRET", secrets.token_urlsafe(48))
    get_settings.cache_clear()
    sent_emails.clear()

    async def fake_send_email(kind: str, *, recipient: str, parameters: dict[str, str]) -> None:
        sent_emails.append((kind, recipient, parameters))

    monkeypatch.setattr("app.auth.service.send_email", fake_send_email)
    monkeypatch.setattr("app.organizations.service.send_email", fake_send_email)

    engine = await make_test_engine(request.param, tmp_path)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    redis = FakeRedis()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[get_channel_cipher] = lambda: ChannelCipher([Fernet.generate_key().decode()])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "sessions": sessions}
    app.dependency_overrides.clear()
    await dispose_test_engine(engine)
    get_settings.cache_clear()
    get_redis_client.cache_clear()


@pytest_asyncio.fixture
async def world(env) -> dict:
    client = env["client"]
    tag = uuid4().hex[:6]
    emails = {role: f"{role}_{tag}@example.com" for role in ("owner", "admin", "editor", "viewer", "outsider")}
    tokens = {role: await create_account(client, email) for role, email in emails.items()}
    org = await client.post("/v1/organizations", json={"name": "Acme", "slug": f"acme-{tag}"}, headers=bearer(tokens["owner"]))
    org_id = org.json()["data"]["id"]
    for role in ("admin", "editor", "viewer"):
        await invite(client, org_id, tokens["owner"], emails[role], role, tokens[role])
    other = await client.post("/v1/organizations", json={"name": "Other", "slug": f"other-{tag}"}, headers=bearer(tokens["outsider"]))
    return {
        **env,
        **tokens,
        "emails": emails,
        "tag": tag,
        "org_id": org_id,
        "other_org": other.json()["data"]["id"],
        "monitor": await create_monitor(client, org_id, tokens["editor"], "Customer API"),
    }


async def logs(world, who="owner", query="") -> dict:
    resp = await world["client"].get(f"{URL}?organization_id={world['org_id']}{query}", headers=bearer(world[who]))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def make_page(world, who="editor") -> dict:
    resp = await world["client"].post(
        "/v1/status-pages",
        json={"organization_id": world["org_id"], "title": "Acme Status", "slug": f"acme-{world['tag']}", "groups": []},
        headers=bearer(world[who]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


# --- what gets recorded -----------------------------------------------------------------


async def test_status_page_changes_are_recorded_newest_first(world):
    client, editor = world["client"], bearer(world["editor"])
    page = await make_page(world)
    await client.patch(f"/v1/status-pages/{page['id']}", json={"title": "Renamed"}, headers=editor)
    await client.delete(f"/v1/status-pages/{page['id']}", headers=bearer(world["owner"]))

    entries = (await logs(world))["data"]
    assert [e["action"] for e in entries] == [
        "status_page.deleted",
        "status_page.updated",
        "status_page.created",
    ]
    assert {e["target_id"] for e in entries} == {page["id"]}
    assert entries[0]["actor"]["email"] == world["emails"]["owner"]
    assert entries[1]["actor"]["email"] == world["emails"]["editor"]
    assert entries[1]["details"] == {"fields": ["title"]}
    assert entries[2]["details"] == {"title": "Acme Status", "slug": f"acme-{world['tag']}"}
    assert entries[2]["actor"]["api_key_id"] is None


async def test_maintenance_changes_are_recorded(world):
    client, editor = world["client"], bearer(world["editor"])
    now = datetime.now(UTC).replace(microsecond=0)
    created = await client.post(
        "/v1/maintenance-windows",
        json={
            "organization_id": world["org_id"],
            "name": "Database upgrade",
            "starts_at": (now + timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(hours=2)).isoformat(),
            "monitor_ids": [world["monitor"]],
        },
        headers=editor,
    )
    window_id = created.json()["data"]["id"]
    await client.patch(f"/v1/maintenance-windows/{window_id}", json={"name": "Bigger upgrade"}, headers=editor)
    await client.delete(f"/v1/maintenance-windows/{window_id}", headers=bearer(world["admin"]))

    entries = (await logs(world, query="&target_id=" + window_id))["data"]
    assert [(e["action"], e["details"].get("name")) for e in entries] == [
        ("maintenance_window.deleted", "Bigger upgrade"),
        ("maintenance_window.updated", None),
        ("maintenance_window.created", "Database upgrade"),
    ]


async def test_channel_entries_never_contain_secrets(world):
    client, editor = world["client"], bearer(world["editor"])
    created = await client.post(
        "/v1/notification-channels",
        json={"organization_id": world["org_id"], "name": "On-call Slack", "type": "slack", "config": {"webhook_url": SLACK_URL}},
        headers=editor,
    )
    channel_id = created.json()["data"]["id"]
    new_url = SLACK_URL.replace("abcdef", "ghijkl")
    await client.patch(f"/v1/notification-channels/{channel_id}", json={"config": {"webhook_url": new_url}}, headers=editor)
    await client.delete(f"/v1/notification-channels/{channel_id}", headers=bearer(world["owner"]))

    body = await logs(world)
    assert [e["action"] for e in body["data"]] == [
        "notification_channel.deleted",
        "notification_channel.updated",
        "notification_channel.created",
    ]
    assert body["data"][1]["details"] == {"fields": ["config"], "secret_rotated": False}
    assert "SECRET" not in json.dumps(body) and "hooks.slack.com" not in json.dumps(body)


async def test_failed_changes_record_nothing(world):
    client, editor = world["client"], bearer(world["editor"])
    await make_page(world)
    # A conflict and a refused request: neither may leave an entry
    await client.post(
        "/v1/status-pages",
        json={"organization_id": world["org_id"], "title": "Again", "slug": f"acme-{world['tag']}", "groups": []},
        headers=editor,
    )
    await client.post(
        "/v1/status-pages",
        json={"organization_id": world["org_id"], "title": "Viewer's", "slug": f"viewer-{world['tag']}", "groups": []},
        headers=bearer(world["viewer"]),
    )
    assert [e["action"] for e in (await logs(world))["data"]] == ["status_page.created"]


async def test_the_request_id_links_an_entry_to_the_server_logs(world):
    resp = await world["client"].post(
        "/v1/status-pages",
        json={"organization_id": world["org_id"], "title": "Acme", "slug": f"acme-{world['tag']}", "groups": []},
        headers={**bearer(world["editor"]), "X-Request-ID": "trace-12345"},
    )
    assert resp.status_code == 201
    assert (await logs(world))["data"][0]["request_id"] == "trace-12345"


# --- who can read ----------------------------------------------------------------------


async def test_only_owners_and_admins_can_read(world):
    await make_page(world)
    for who in ("owner", "admin"):
        assert len((await logs(world, who))["data"]) == 1
    for who, status in (("editor", 403), ("viewer", 403), ("outsider", 404)):
        resp = await world["client"].get(f"{URL}?organization_id={world['org_id']}", headers=bearer(world[who]))
        assert resp.status_code == status


async def test_organizations_never_see_each_others_entries(world):
    await make_page(world)
    resp = await world["client"].get(f"{URL}?organization_id={world['other_org']}", headers=bearer(world["outsider"]))
    assert resp.json()["data"] == []


async def test_changes_through_an_api_key_name_the_key(world):
    async def key(scopes):
        resp = await world["client"].post(
            "/v1/api-keys",
            json={"organization_id": world["org_id"], "name": f"k-{secrets.token_hex(3)}", "scopes": scopes},
            headers=bearer(world["owner"]),
        )
        data = resp.json()["data"]
        return data["id"], {"Authorization": f"Bearer {data['key']}"}

    key_id, headers = await key(["status_pages:write"])
    resp = await world["client"].post(
        "/v1/status-pages",
        json={"organization_id": world["org_id"], "title": "Bot page", "slug": f"bot-{world['tag']}", "groups": []},
        headers=headers,
    )
    assert resp.status_code == 201
    entry = (await logs(world))["data"][0]
    assert entry["actor"]["api_key_id"] == key_id

    _, cannot_read = await key(["status_pages:read"])
    url = f"{URL}?organization_id={world['org_id']}"
    assert (await world["client"].get(url, headers=cannot_read)).status_code == 403
    _, can_read = await key(["audit_logs:read"])
    assert (await world["client"].get(url, headers=can_read)).status_code == 200


# --- filters and pages -------------------------------------------------------------------


async def test_filter_by_action_and_page_through(world):
    client, editor = world["client"], bearer(world["editor"])
    page = await make_page(world)
    for i in range(4):
        await client.patch(f"/v1/status-pages/{page['id']}", json={"title": f"Title {i}"}, headers=editor)

    only_updates = (await logs(world, query="&action=status_page.updated"))["data"]
    assert len(only_updates) == 4

    seen: list[str] = []
    query = "&limit=2"
    while True:
        body = await logs(world, query=query)
        seen += [e["id"] for e in body["data"]]
        if body["next_before"] is None:
            break
        query = "&limit=2&before=" + body["next_before"].replace("+", "%2B")
    assert len(seen) == len(set(seen)) == 5


@pytest.mark.parametrize("query", ["&limit=0", "&limit=201", "&before=yesterday", "&before=2026-11-27T10:00:00"])
async def test_bad_parameters(world, query):
    resp = await world["client"].get(f"{URL}?organization_id={world['org_id']}{query}", headers=bearer(world["owner"]))
    assert resp.status_code == 422
