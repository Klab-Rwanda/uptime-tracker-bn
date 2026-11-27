"""API keys may only do what their scopes allow, only in their own organization,
and never the things only a person should do (keys, members, ownership)."""

import secrets
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.api_key_access import KNOWN_SCOPES
from app.auth.errors import APIError
from app.main import app
from app.util.config import get_settings
from app.util.database import get_db_session
from app.util.redis import get_redis_client
from app.websocket.router import _authenticate_ws
from tests.conftest import dispose_test_engine, make_test_engine
from tests.test_status_pages_api import (
    FakeRedis,
    bearer,
    create_account,
    invite,
    sent_emails,
)


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
    monkeypatch.setattr("app.websocket.router.sessionLocal", sessions)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    redis = FakeRedis()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client}
    app.dependency_overrides.clear()
    await dispose_test_engine(engine)
    get_settings.cache_clear()
    get_redis_client.cache_clear()


@pytest_asyncio.fixture
async def world(env) -> dict:
    """The owner belongs to two organizations; keys are made for the first."""
    client = env["client"]
    tag = uuid4().hex[:6]
    owner = await create_account(client, f"owner_{tag}@example.com")
    member = await create_account(client, f"member_{tag}@example.com")
    org = await client.post("/v1/organizations", json={"name": "Acme", "slug": f"acme-{tag}"}, headers=bearer(owner))
    other = await client.post("/v1/organizations", json={"name": "Secret Co", "slug": f"secret-{tag}"}, headers=bearer(owner))
    org_id = org.json()["data"]["id"]
    await invite(client, org_id, owner, f"member_{tag}@example.com", "admin", member)
    monitor = await client.post(
        "/v1/monitors",
        json={"organization_id": org_id, "name": "API", "type": "http", "target": "https://example.com"},
        headers=bearer(owner),
    )
    project = await client.post("/v1/projects", json={"organization_id": org_id, "name": "Web"}, headers=bearer(owner))
    assert (monitor.status_code, project.status_code) == (201, 201), (monitor.text, project.text)
    return {
        **env,
        "tag": tag,
        "owner": owner,
        "member": member,
        "org_id": org_id,
        "other_org": other.json()["data"]["id"],
        "monitor": monitor.json()["data"]["id"],
        "project": project.json()["data"]["id"],
    }


async def make_key(world, scopes, who="owner") -> dict:
    resp = await world["client"].post(
        "/v1/api-keys",
        json={"organization_id": world["org_id"], "name": f"k-{secrets.token_hex(3)}", "scopes": scopes},
        headers=bearer(world[who]),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    return {"id": data["id"], "token": data["key"], "headers": {"Authorization": f"Bearer {data['key']}"}}


async def call(world, key, method, url, json=None):
    return await world["client"].request(method, url, json=json, headers=key["headers"])


def assert_scope_missing(resp, scope):
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "insufficient_scope"
    assert scope in resp.json()["error"]["message"]


# --- monitors, projects, dashboard ---------------------------------------------------


def monitor_body(world):
    return {"organization_id": world["org_id"], "name": f"M {uuid4().hex[:4]}", "type": "http", "target": "https://example.org"}


async def test_a_key_without_monitor_scopes_cannot_touch_monitors(world):
    key = await make_key(world, ["status_pages:read"])
    m = world["monitor"]
    for method, url, body in [
        ("GET", f"/v1/monitors?organization_id={world['org_id']}", None),
        ("GET", f"/v1/monitors/{m}", None),
        ("GET", f"/v1/dashboard/summary?organization_id={world['org_id']}", None),
    ]:
        assert_scope_missing(await call(world, key, method, url, body), "monitors:read")
    for method, url, body in [
        ("POST", "/v1/monitors", monitor_body(world)),
        ("PATCH", f"/v1/monitors/{m}", {"name": "Hacked"}),
        ("POST", f"/v1/monitors/{m}/pause", None),
        ("POST", f"/v1/monitors/{m}/resume", None),
        ("POST", f"/v1/monitors/{m}/test", None),
        ("DELETE", f"/v1/monitors/{m}", None),
        ("POST", "/checks/run", {"target": "https://example.com"}),
    ]:
        assert_scope_missing(await call(world, key, method, url, body), "monitors:write")


async def test_read_scope_reads_but_cannot_change(world):
    key = await make_key(world, ["monitors:read"])
    assert (await call(world, key, "GET", f"/v1/monitors?organization_id={world['org_id']}")).status_code == 200
    assert (await call(world, key, "GET", f"/v1/monitors/{world['monitor']}")).status_code == 200
    assert (await call(world, key, "GET", f"/v1/dashboard/summary?organization_id={world['org_id']}")).status_code == 200
    assert_scope_missing(await call(world, key, "DELETE", f"/v1/monitors/{world['monitor']}"), "monitors:write")


async def test_write_scope_manages_monitors(world):
    key = await make_key(world, ["monitors:write"])
    created = await call(world, key, "POST", "/v1/monitors", monitor_body(world))
    assert created.status_code == 201, created.text
    monitor_id = created.json()["data"]["id"]
    assert (await call(world, key, "PATCH", f"/v1/monitors/{monitor_id}", {"name": "Renamed"})).status_code == 200
    assert (await call(world, key, "POST", f"/v1/monitors/{monitor_id}/pause")).status_code == 200
    assert (await call(world, key, "DELETE", f"/v1/monitors/{monitor_id}")).status_code == 204


async def test_project_scopes(world):
    p = world["project"]
    none = await make_key(world, ["monitors:read"])
    assert_scope_missing(await call(world, none, "GET", f"/v1/projects?organization_id={world['org_id']}"), "projects:read")
    assert_scope_missing(await call(world, none, "GET", f"/v1/projects/{p}"), "projects:read")

    read = await make_key(world, ["projects:read"])
    assert (await call(world, read, "GET", f"/v1/projects/{p}")).status_code == 200
    assert_scope_missing(await call(world, read, "PATCH", f"/v1/projects/{p}", {"name": "X"}), "projects:write")
    assert_scope_missing(
        await call(world, read, "POST", "/v1/projects", {"organization_id": world["org_id"], "name": "New"}),
        "projects:write",
    )

    write = await make_key(world, ["projects:write"])
    assert (await call(world, write, "PATCH", f"/v1/projects/{p}", {"name": "Renamed"})).status_code == 200
    assert (await call(world, write, "DELETE", f"/v1/projects/{p}")).status_code == 204


# --- organizations ----------------------------------------------------------------------------


async def test_a_key_lists_only_its_own_organization(world):
    key = await make_key(world, ["organizations:read"])
    listed = await call(world, key, "GET", "/v1/organizations")
    assert [o["id"] for o in listed.json()["data"]] == [world["org_id"]]  # never "Secret Co"
    assert "Secret Co" not in listed.text
    assert (await call(world, key, "GET", f"/v1/organizations/{world['org_id']}")).status_code == 200
    assert (await call(world, key, "GET", f"/v1/organizations/{world['org_id']}/members")).status_code == 200
    assert (await call(world, key, "GET", f"/v1/organizations/{world['other_org']}")).status_code == 404

    # A person's login still sees both
    mine = await world["client"].get("/v1/organizations", headers=bearer(world["owner"]))
    assert len(mine.json()["data"]) == 2


async def test_organization_reads_need_the_scope(world):
    key = await make_key(world, ["monitors:read"])
    for url in ("/v1/organizations", f"/v1/organizations/{world['org_id']}", f"/v1/organizations/{world['org_id']}/members"):
        assert_scope_missing(await call(world, key, "GET", url), "organizations:read")


# --- things only a person may do ------------------------------------------------------------


async def test_keys_can_never_manage_keys_members_or_ownership(world):
    key = await make_key(world, sorted(KNOWN_SCOPES))  # even with every scope
    org = world["org_id"]
    for method, url, body in [
        ("POST", "/v1/api-keys", {"organization_id": org, "name": "child", "scopes": sorted(KNOWN_SCOPES)}),
        ("GET", f"/v1/api-keys?organization_id={org}", None),
        ("DELETE", f"/v1/api-keys/{key['id']}", None),
        ("POST", "/v1/organizations", {"name": "New org"}),
        ("PATCH", f"/v1/organizations/{org}", {"name": "Renamed"}),
        ("POST", f"/v1/organizations/{org}/transfer-ownership", {"new_owner_user_id": str(uuid4())}),
        ("POST", f"/v1/organizations/{org}/members", {"email": "x@example.com", "role": "admin"}),
        ("POST", f"/v1/organizations/{org}/invitations", {"email": "x@example.com", "role": "admin"}),
        ("GET", f"/v1/organizations/{org}/invitations", None),
        ("DELETE", f"/v1/organizations/{org}/invitations/{uuid4()}", None),
        ("DELETE", f"/v1/organizations/{org}/members/me", None),
        ("DELETE", f"/v1/organizations/{org}/members/{uuid4()}", None),
        ("POST", "/v1/invitations/accept", {"token": "x" * 40}),
        ("POST", "/v1/organizations/invitations/accept", {"token": "x" * 40}),
        ("POST", "/v1/auth/logout", None),
    ]:
        resp = await call(world, key, method, url, body)
        assert resp.status_code == 403, (method, url, resp.text)
        assert resp.json()["error"]["code"] == "api_key_not_allowed", (method, url)

    # The key still works and the owner's account is untouched
    assert (await call(world, key, "GET", f"/v1/monitors?organization_id={org}")).status_code == 200
    keys = await world["client"].get(f"/v1/api-keys?organization_id={org}", headers=bearer(world["owner"]))
    assert len(keys.json()["data"]) == 1


async def test_people_keep_full_access(world):
    owner, org = bearer(world["owner"]), world["org_id"]
    assert (await world["client"].get(f"/v1/monitors?organization_id={org}", headers=owner)).status_code == 200
    assert (await world["client"].patch(f"/v1/organizations/{org}", json={"name": "Acme Inc"}, headers=owner)).status_code == 200
    assert (await world["client"].get(f"/v1/api-keys?organization_id={org}", headers=owner)).status_code == 200


# --- creating keys --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scopes",
    [["monitor:read"], ["monitors:read", "admin"], ["*"], [], ["x" * 500]],
)
async def test_unknown_scopes_are_refused(world, scopes):
    resp = await world["client"].post(
        "/v1/api-keys",
        json={"organization_id": world["org_id"], "name": "typo", "scopes": scopes},
        headers=bearer(world["owner"]),
    )
    assert resp.status_code == 422
    assert "x" * 100 not in resp.text  # long input is not echoed back


async def test_scopes_are_deduplicated_and_default_to_monitors(world):
    dup = await make_key(world, ["monitors:read", "monitors:read", "projects:read"])
    listed = await world["client"].get(f"/v1/api-keys?organization_id={world['org_id']}", headers=bearer(world["owner"]))
    [entry] = [k for k in listed.json()["data"] if k["id"] == dup["id"]]
    assert entry["scopes"] == ["monitors:read", "projects:read"]

    default = await world["client"].post(
        "/v1/api-keys", json={"organization_id": world["org_id"], "name": "default"}, headers=bearer(world["owner"])
    )
    assert default.json()["data"]["scopes"] == ["monitors:read", "monitors:write"]


# --- live updates (WebSocket) ---------------------------------------------------------------


async def test_websocket_keys_need_monitors_read(world):
    org = UUID(world["org_id"])
    without = await make_key(world, ["status_pages:read"])
    with pytest.raises(ValueError, match="monitors:read"):
        await _authenticate_ws(without["token"], org)

    with_scope = await make_key(world, ["monitors:read"])
    user = await _authenticate_ws(with_scope["token"], org)
    assert user.email == f"owner_{world['tag']}@example.com"


async def test_websocket_key_stops_working_when_its_creator_leaves(world):
    key = await make_key(world, ["monitors:read"], who="member")
    org = UUID(world["org_id"])
    assert await _authenticate_ws(key["token"], org)

    left = await world["client"].delete(f"/v1/organizations/{world['org_id']}/members/me", headers=bearer(world["member"]))
    assert left.status_code == 204, left.text
    with pytest.raises(APIError):  # not a member anymore
        await _authenticate_ws(key["token"], org)
