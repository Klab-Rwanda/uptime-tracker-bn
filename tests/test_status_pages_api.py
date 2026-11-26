import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.incidents.service import acknowledge_incident, open_incident, resolve_incident
from app.main import app
from app.models.monitor import Monitor
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import get_channel_cipher
from app.status_pages import cache
from app.util.config import get_settings
from app.util.database import get_db_session
from app.util.redis import get_redis_client
from tests.conftest import dispose_test_engine, make_test_engine
from tests.helpers.tenant import assert_tenant_isolated

sent_emails: list[tuple[str, str, dict[str, str]]] = []
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)


class FakeRedis:
    """In-memory stand-in for the few Redis commands the app uses."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.expiry: dict[str, int | None] = {}
        self.calls = 0

    async def eval(self, *_: object) -> int:
        return 1

    async def get(self, key: str) -> bytes | None:
        self.calls += 1
        return self.data.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self.calls += 1
        self.data[key] = value
        self.expiry[key] = ex

    async def delete(self, *keys: str) -> None:
        self.calls += 1
        for key in keys:
            self.data.pop(key, None)


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

    engine = await make_test_engine(request.param, tmp_path)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    redis = FakeRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[get_channel_cipher] = lambda: ChannelCipher([Fernet.generate_key().decode()])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "sessions": sessions, "redis": redis}
    app.dependency_overrides.clear()
    await dispose_test_engine(engine)
    get_settings.cache_clear()
    get_redis_client.cache_clear()


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_account(client: AsyncClient, email: str) -> dict[str, str]:
    password = "StrongPassword123!"
    await client.post("/v1/auth/register", json={"email": email, "password": password, "full_name": "T"})
    _, _, params = next(m for m in reversed(sent_emails) if m[1] == email.lower() and m[0] == "otp_code")
    await client.post("/v1/auth/verify-email", json={"email": email, "code": params["otp_code"]})
    return (await client.post("/v1/auth/login", json={"email": email, "password": password})).json()["data"]


async def invite(client, org_id, owner, email, role, tokens) -> None:
    await client.post(
        f"/v1/organizations/{org_id}/invitations", json={"email": email, "role": role}, headers=bearer(owner)
    )
    msg = next(m for m in reversed(sent_emails) if m[0] == "invite" and m[1] == email)
    token = msg[2]["invite_url"].split("#token=")[1]
    await client.post("/v1/invitations/accept", json={"token": token}, headers=bearer(tokens))


async def create_monitor(client, org_id, tokens, name) -> str:
    resp = await client.post(
        "/v1/monitors",
        json={
            "organization_id": org_id,
            "name": name,
            "type": "http",
            "target": f"https://internal-{name.lower().replace(' ', '-')}.acme.com/health?token=SECRET",
        },
        headers=bearer(tokens),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


@pytest_asyncio.fixture
async def world(env) -> dict:
    client = env["client"]
    tag = uuid4().hex[:6]
    owner = await create_account(client, f"owner_{tag}@example.com")
    editor = await create_account(client, f"editor_{tag}@example.com")
    viewer = await create_account(client, f"viewer_{tag}@example.com")
    outsider = await create_account(client, f"out_{tag}@example.com")
    org = await client.post("/v1/organizations", json={"name": "Acme", "slug": f"acme-{tag}"}, headers=bearer(owner))
    org_id = org.json()["data"]["id"]
    await invite(client, org_id, owner, f"editor_{tag}@example.com", "editor", editor)
    await invite(client, org_id, owner, f"viewer_{tag}@example.com", "viewer", viewer)
    other = await client.post("/v1/organizations", json={"name": "Other", "slug": f"other-{tag}"}, headers=bearer(outsider))
    other_org = other.json()["data"]["id"]
    return {
        **env,
        "tag": tag,
        "org_id": org_id,
        "other_org": other_org,
        "owner": owner,
        "editor": editor,
        "viewer": viewer,
        "outsider": outsider,
        "api": await create_monitor(client, org_id, editor, "Customer API"),
        "web": await create_monitor(client, org_id, editor, "Website"),
        "db": await create_monitor(client, org_id, editor, "Database"),
        "foreign": await create_monitor(client, other_org, outsider, "Their API"),
    }


def page_body(world, **overrides) -> dict:
    body = {
        "organization_id": world["org_id"],
        "title": "Acme Status",
        "slug": f"acme-{world['tag']}",
        "groups": [
            {"name": "Customer-facing services", "monitor_ids": [world["api"], world["web"]]},
            {"name": "Internal", "monitor_ids": [world["db"]]},
        ],
    }
    body.update(overrides)
    return body


async def create_page(world, **overrides) -> dict:
    resp = await world["client"].post(
        "/v1/status-pages", json=page_body(world, **overrides), headers=bearer(world["editor"])
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def set_monitor(world, key, *, status=None, active=None):
    async with world["sessions"]() as s:
        monitor = await s.get(Monitor, UUID(world[key]))
        if status is not None:
            monitor.current_status = status
        if active is not None:
            monitor.is_active = active
        await s.commit()


# --- management -----------------------------------------------------------------------


async def test_create_page_with_groups(world):
    page = await create_page(world, custom_domain="Status.Acme.COM")
    assert page["slug"] == f"acme-{world['tag']}"
    assert page["custom_domain"] == "status.acme.com"
    assert page["url"] == f"https://app.example.test/status/acme-{world['tag']}"
    assert page["groups"] == [
        {"name": "Customer-facing services", "monitor_ids": [world["api"], world["web"]]},
        {"name": "Internal", "monitor_ids": [world["db"]]},
    ]


async def test_viewer_cannot_create_and_outsider_cannot_use_the_org(world):
    client = world["client"]
    assert (await client.post("/v1/status-pages", json=page_body(world), headers=bearer(world["viewer"]))).status_code == 403
    assert (await client.post("/v1/status-pages", json=page_body(world), headers=bearer(world["outsider"]))).status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"slug": "ab"},
        {"slug": "-acme"},
        {"slug": "acme-"},
        {"slug": "ac me"},
        {"slug": "acme/../x"},
        {"slug": "api"},
        {"custom_domain": "https://status.acme.com"},
        {"custom_domain": "status.acme.com/path"},
        {"custom_domain": "localhost"},
        {"title": "   "},
        {"groups": [{"name": "", "monitor_ids": []}]},
        {"extra": "field"},
    ],
)
async def test_bad_input_is_rejected(world, overrides):
    resp = await world["client"].post(
        "/v1/status-pages", json=page_body(world, **overrides), headers=bearer(world["editor"])
    )
    assert resp.status_code == 422


async def test_a_monitor_can_appear_only_once(world):
    groups = [{"name": "A", "monitor_ids": [world["api"]]}, {"name": "B", "monitor_ids": [world["api"]]}]
    resp = await world["client"].post(
        "/v1/status-pages", json=page_body(world, groups=groups), headers=bearer(world["editor"])
    )
    assert resp.status_code == 422


async def test_monitors_of_another_organization_are_refused(world):
    groups = [{"name": "Stolen", "monitor_ids": [world["foreign"]]}]
    resp = await world["client"].post(
        "/v1/status-pages", json=page_body(world, groups=groups), headers=bearer(world["editor"])
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "monitor_not_found"


async def test_slug_and_domain_are_unique_across_organizations(world):
    await create_page(world, custom_domain="status.acme.com")
    client, theirs = world["client"], bearer(world["outsider"])
    base = {"organization_id": world["other_org"], "title": "Theirs", "groups": []}
    taken_slug = await client.post("/v1/status-pages", json={**base, "slug": f"acme-{world['tag']}"}, headers=theirs)
    taken_domain = await client.post(
        "/v1/status-pages", json={**base, "slug": f"theirs-{world['tag']}", "custom_domain": "status.acme.com"}, headers=theirs
    )
    assert (taken_slug.status_code, taken_domain.status_code) == (409, 409)


async def test_members_list_and_get_and_outsiders_see_nothing(world):
    page = await create_page(world)
    client = world["client"]
    listed = await client.get(f"/v1/status-pages?organization_id={world['org_id']}", headers=bearer(world["viewer"]))
    assert [p["id"] for p in listed.json()["data"]] == [page["id"]]
    assert (await client.get(f"/v1/status-pages/{page['id']}", headers=bearer(world["viewer"]))).status_code == 200

    outsider = bearer(world["outsider"])
    for method, url in [
        ("GET", f"/v1/status-pages/{page['id']}"),
        ("PATCH", f"/v1/status-pages/{page['id']}"),
        ("DELETE", f"/v1/status-pages/{page['id']}"),
        ("GET", f"/v1/status-pages?organization_id={world['org_id']}"),
    ]:
        await assert_tenant_isolated(client, method, url, outsider_headers=outsider, json={"title": "x"} if method == "PATCH" else None)
    resp = await client.get(f"/v1/status-pages/{page['id']}", headers=outsider)
    assert resp.json()["error"]["code"] == "status_page_not_found"


async def test_update_title_slug_domain_and_replace_groups(world):
    page = await create_page(world, custom_domain="status.acme.com")
    new_slug = f"acme-new-{world['tag']}"
    resp = await world["client"].patch(
        f"/v1/status-pages/{page['id']}",
        json={
            "title": "Acme Platform",
            "slug": new_slug,
            "custom_domain": None,
            "groups": [{"name": "Everything", "monitor_ids": [world["db"], world["api"]]}],
        },
        headers=bearer(world["editor"]),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert (data["title"], data["slug"], data["custom_domain"]) == ("Acme Platform", new_slug, None)
    assert data["groups"] == [{"name": "Everything", "monitor_ids": [world["db"], world["api"]]}]


async def test_update_keeps_what_was_not_sent(world):
    page = await create_page(world, custom_domain="status.acme.com")
    resp = await world["client"].patch(
        f"/v1/status-pages/{page['id']}", json={"title": "Renamed"}, headers=bearer(world["editor"])
    )
    data = resp.json()["data"]
    assert (data["title"], data["custom_domain"], len(data["groups"])) == ("Renamed", "status.acme.com", 2)


@pytest.mark.parametrize("body", [{}, {"title": None}, {"slug": None}, {"slug": "bad slug"}])
async def test_bad_updates_are_rejected(world, body):
    page = await create_page(world)
    resp = await world["client"].patch(f"/v1/status-pages/{page['id']}", json=body, headers=bearer(world["editor"]))
    assert resp.status_code == 422


async def test_only_owner_or_admin_can_delete(world):
    page = await create_page(world)
    client, url = world["client"], f"/v1/status-pages/{page['id']}"
    assert (await client.delete(url, headers=bearer(world["editor"]))).status_code == 403
    assert (await client.delete(url, headers=bearer(world["owner"]))).status_code == 204
    assert (await client.get(f"/status/acme-{world['tag']}")).status_code == 404


async def test_deleting_a_monitor_removes_it_from_pages(world):
    page = await create_page(world)
    deleted = await world["client"].delete(f"/v1/monitors/{world['web']}", headers=bearer(world["owner"]))
    assert deleted.status_code == 204, deleted.text
    data = (await world["client"].get(f"/v1/status-pages/{page['id']}", headers=bearer(world["viewer"]))).json()["data"]
    assert data["groups"][0]["monitor_ids"] == [world["api"]]


async def test_api_keys_need_status_page_scopes(world):
    async def key(scopes):
        resp = await world["client"].post(
            "/v1/api-keys",
            json={"organization_id": world["org_id"], "name": f"k-{secrets.token_hex(3)}", "scopes": scopes},
            headers=bearer(world["owner"]),
        )
        return {"Authorization": f"Bearer {resp.json()['data']['key']}"}

    client, list_url = world["client"], f"/v1/status-pages?organization_id={world['org_id']}"
    assert (await client.get(list_url, headers=await key(["monitors:read"]))).status_code == 403
    assert (await client.get(list_url, headers=await key(["status_pages:read"]))).status_code == 200
    write = await client.post("/v1/status-pages", json=page_body(world), headers=await key(["status_pages:write"]))
    assert write.status_code == 201


# --- the public page ------------------------------------------------------------------


async def public(world, slug=None):
    return await world["client"].get(f"/status/{slug or 'acme-' + world['tag']}")


async def test_public_page_needs_no_login_and_shows_statuses(world):
    await create_page(world)
    await set_monitor(world, "api", status="down")
    await set_monitor(world, "web", status="up")
    await set_monitor(world, "db", status="up")

    resp = await public(world)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=30"
    data = resp.json()["data"]
    assert data["title"] == "Acme Status"
    assert data["overall_status"] == "partial_outage"
    assert data["groups"] == [
        {
            "name": "Customer-facing services",
            "status": "partial_outage",
            "monitors": [{"name": "Customer API", "status": "down"}, {"name": "Website", "status": "up"}],
        },
        {"name": "Internal", "status": "operational", "monitors": [{"name": "Database", "status": "up"}]},
    ]


async def test_public_page_never_leaks_private_details(world):
    page = await create_page(world)
    await set_monitor(world, "api", status="down")
    async with world["sessions"]() as s:
        monitor = await s.get(Monitor, UUID(world["api"]))
        await open_incident(s, monitor, started_at=T0, cause="Connection refused by 10.0.0.5", now=T0)

    text = (await public(world)).text
    for private in ("SECRET", "internal-", "10.0.0.5", "Connection refused", world["org_id"], world["api"], page["id"]):
        assert private not in text


async def test_paused_monitors_show_as_paused_and_do_not_cause_outages(world):
    await create_page(world)
    await set_monitor(world, "api", status="down", active=False)
    for key in ("web", "db"):
        await set_monitor(world, key, status="up")
    data = (await public(world)).json()["data"]
    assert data["groups"][0]["monitors"][0] == {"name": "Customer API", "status": "paused"}
    assert data["overall_status"] == "operational"


async def test_active_incidents_of_page_monitors_only(world):
    await create_page(world, groups=[{"name": "Main", "monitor_ids": [world["api"], world["web"]]}])
    async with world["sessions"]() as s:
        api = await s.get(Monitor, UUID(world["api"]))
        web = await s.get(Monitor, UUID(world["web"]))
        db = await s.get(Monitor, UUID(world["db"]))
        investigating, _ = await open_incident(s, api, started_at=T0, now=T0)
        identified, _ = await open_incident(s, web, started_at=T0 + timedelta(minutes=5), now=T0)
        await acknowledge_incident(s, identified.id, actor_user_id=None, now=T0 + timedelta(minutes=6))
        await open_incident(s, db, started_at=T0, now=T0)  # not on the page

    incidents = (await public(world)).json()["data"]["active_incidents"]
    assert [(i["title"], i["status"]) for i in incidents] == [
        ("Website is down", "identified"),
        ("Customer API is down", "investigating"),
    ]

    async with world["sessions"]() as s:
        await resolve_incident(s, investigating.id)
    world["redis"].data.clear()  # the cache would hide the change for a few seconds
    titles = [i["title"] for i in (await public(world)).json()["data"]["active_incidents"]]
    assert titles == ["Website is down"]


async def test_unknown_and_invalid_slugs_are_404(world):
    for slug in ("does-not-exist", "Bad Slug!", "a" * 100):
        resp = await public(world, slug)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "status_page_not_found"


async def test_slug_is_case_insensitive(world):
    await create_page(world)
    assert (await public(world, f"ACME-{world['tag']}")).status_code == 200


async def test_an_empty_page_is_operational(world):
    await create_page(world, groups=[])
    data = (await public(world)).json()["data"]
    assert (data["overall_status"], data["groups"], data["active_incidents"]) == ("operational", [], [])


async def test_an_unexpected_stored_status_does_not_break_the_page(world):
    await create_page(world)
    await set_monitor(world, "api", status="weird-value")
    resp = await public(world)
    assert resp.status_code == 200
    assert resp.json()["data"]["groups"][0]["monitors"][0]["status"] == "pending"


# --- the public page cache --------------------------------------------------------


class BrokenRedis(FakeRedis):
    async def get(self, key: str) -> bytes | None:
        raise RedisError("redis is down")

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        raise RedisError("redis is down")

    async def delete(self, *keys: str) -> None:
        raise RedisError("redis is down")


class SlowRedis(FakeRedis):
    async def get(self, key: str) -> bytes | None:
        await asyncio.sleep(5)
        return None

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        await asyncio.sleep(5)

    async def delete(self, *keys: str) -> None:
        await asyncio.sleep(5)


def use_redis(fake: FakeRedis) -> None:
    app.dependency_overrides[get_redis_client] = lambda: fake


async def test_second_visit_is_served_from_the_cache(world):
    await create_page(world)
    await set_monitor(world, "api", status="up")
    first = await public(world)
    assert first.headers["x-cache"] == "MISS"
    assert world["redis"].expiry[cache.cache_key(f"acme-{world['tag']}")] == cache.CACHE_SECONDS

    # The database changes, but for a few seconds visitors get the cached copy
    await set_monitor(world, "api", status="down")
    second = await public(world)
    assert second.headers["x-cache"] == "HIT"
    assert second.json() == first.json()
    assert second.headers["cache-control"] == "public, max-age=30"


async def test_editing_a_page_clears_its_cache(world):
    page = await create_page(world)
    await public(world)
    await world["client"].patch(
        f"/v1/status-pages/{page['id']}", json={"title": "Renamed"}, headers=bearer(world["editor"])
    )
    resp = await public(world)
    assert (resp.headers["x-cache"], resp.json()["data"]["title"]) == ("MISS", "Renamed")


async def test_changing_the_slug_clears_the_old_address(world):
    page = await create_page(world)
    old = f"acme-{world['tag']}"
    assert (await public(world, old)).status_code == 200
    new = f"acme-moved-{world['tag']}"
    await world["client"].patch(
        f"/v1/status-pages/{page['id']}", json={"slug": new}, headers=bearer(world["editor"])
    )
    assert (await public(world, old)).status_code == 404
    assert (await public(world, new)).status_code == 200


async def test_deleting_a_page_clears_its_cache(world):
    page = await create_page(world)
    await public(world)
    await world["client"].delete(f"/v1/status-pages/{page['id']}", headers=bearer(world["owner"]))
    assert (await public(world)).status_code == 404


async def test_unknown_slugs_are_cached_and_creating_the_page_clears_that(world):
    slug = f"acme-{world['tag']}"
    assert (await public(world, slug)).status_code == 404
    assert world["redis"].data[cache.cache_key(slug)] == b"-"
    assert (await public(world, slug)).status_code == 404  # answered by the cache

    await create_page(world)
    resp = await public(world, slug)
    assert (resp.status_code, resp.headers["x-cache"]) == (200, "MISS")


async def test_invalid_slugs_never_reach_redis(world):
    world["redis"].calls = 0
    assert (await public(world, "Bad Slug!")).status_code == 404
    assert world["redis"].calls == 0


async def test_a_broken_cache_entry_is_rebuilt(world):
    await create_page(world)
    world["redis"].data[cache.cache_key(f"acme-{world['tag']}")] = b'{"not": "a page"}'
    resp = await public(world)
    assert (resp.status_code, resp.headers["x-cache"]) == (200, "MISS")


@pytest.mark.parametrize("fake", [BrokenRedis, SlowRedis])
async def test_the_page_works_when_redis_is_down_or_slow(world, monkeypatch, fake):
    monkeypatch.setattr(cache, "REDIS_TIMEOUT_SECONDS", 0.05)
    page = await create_page(world)
    use_redis(fake())

    started = time.monotonic()
    resp = await public(world)
    assert (resp.status_code, resp.headers["x-cache"]) == (200, "MISS")
    # Editing still works; the cache just could not be cleared
    edit = await world["client"].patch(
        f"/v1/status-pages/{page['id']}", json={"title": "Renamed"}, headers=bearer(world["editor"])
    )
    assert edit.status_code == 200
    assert time.monotonic() - started < 3
