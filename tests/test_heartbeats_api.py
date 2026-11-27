import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.heartbeats.service import encode_cursor
from app.main import app
from app.models.heartbeat import Heartbeat
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

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


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
    owner = await create_account(client, f"owner_{tag}@example.com")
    viewer = await create_account(client, f"viewer_{tag}@example.com")
    outsider = await create_account(client, f"out_{tag}@example.com")
    org = await client.post("/v1/organizations", json={"name": "Acme", "slug": f"acme-{tag}"}, headers=bearer(owner))
    org_id = org.json()["data"]["id"]
    await invite(client, org_id, owner, f"viewer_{tag}@example.com", "viewer", viewer)
    return {
        **env,
        "org_id": org_id,
        "owner": owner,
        "viewer": viewer,
        "outsider": outsider,
        "api": await create_monitor(client, org_id, owner, "Customer API"),
        "web": await create_monitor(client, org_id, owner, "Website"),
    }


async def add_heartbeats(world, key: str, statuses: list[str], *, start=T0, step=timedelta(minutes=1)) -> list[str]:
    """Store one heartbeat per status, one `step` apart; returns their IDs."""
    ids = []
    async with world["sessions"]() as s:
        for i, status in enumerate(statuses):
            beat = Heartbeat(
                monitor_id=UUID(world[key]),
                status=status,
                response_time_ms=None if status == "down" else 100 + i,
                checked_at=start + step * i,
                error_message="Unexpected status code: 503" if status == "down" else None,
            )
            s.add(beat)
            await s.flush()
            ids.append(str(beat.id))
        await s.commit()
    return ids


async def get(world, path: str, who="viewer", headers=None):
    return await world["client"].get(path, headers=headers or bearer(world[who]))


def url(world, key="api", query=""):
    return f"/v1/monitors/{world[key]}/heartbeats{query}"


# --- listing --------------------------------------------------------------------------


async def test_newest_first_with_all_fields(world):
    await add_heartbeats(world, "api", ["up", "down"])
    await add_heartbeats(world, "web", ["up"] * 3)  # another monitor: never mixed in

    body = (await get(world, url(world))).json()
    assert [h["status"] for h in body["data"]] == ["down", "up"]
    newest = body["data"][0]
    assert newest["monitor_id"] == world["api"]
    assert newest["message"] == "Unexpected status code: 503"
    assert newest["response_time_ms"] is None
    assert newest["checked_at"].startswith("2026-11-27T12:01:00")
    assert body["data"][1]["response_time_ms"] == 100
    assert body["meta"]["next_cursor"] is None


async def test_paging_returns_everything_exactly_once(world):
    # Two heartbeats share each timestamp: the cursor must still not skip or repeat
    first = await add_heartbeats(world, "api", ["up"] * 7)
    second = await add_heartbeats(world, "api", ["down"] * 7)
    seen, cursor = [], None
    while True:
        query = "?limit=3" + (f"&cursor={cursor}" if cursor else "")
        body = (await get(world, url(world, query=query))).json()
        seen += [h["id"] for h in body["data"]]
        cursor = body["meta"]["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(first + second)
    assert len(seen) == len(set(seen)) == 14


async def test_filter_by_status_and_time(world):
    await add_heartbeats(world, "api", ["up", "down", "up", "down", "up"])
    downs = (await get(world, url(world, query="?status=down"))).json()["data"]
    assert [h["status"] for h in downs] == ["down", "down"]

    since = (T0 + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    until = (T0 + timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
    window = (await get(world, url(world, query=f"?since={since}&until={until}"))).json()["data"]
    assert [h["checked_at"][:16] for h in window] == ["2026-11-27T12:02", "2026-11-27T12:01"]


@pytest.mark.parametrize(
    "query",
    ["?limit=0", "?limit=501", "?status=DOWN", "?status=broken", "?since=yesterday", "?since=2026-11-27T12:00:00", "?cursor=not-a-cursor"],
)
async def test_bad_parameters(world, query):
    assert (await get(world, url(world, query=query))).status_code == 422


async def test_a_cursor_from_another_monitor_finds_nothing_foreign(world):
    web_ids = await add_heartbeats(world, "web", ["up", "up"])
    await add_heartbeats(world, "api", ["up"])
    async with world["sessions"]() as s:
        web_beat = await s.get(Heartbeat, UUID(web_ids[1]))
    body = (await get(world, url(world, query=f"?cursor={encode_cursor(web_beat)}"))).json()
    assert all(h["monitor_id"] == world["api"] for h in body["data"])


async def test_empty_history(world):
    body = (await get(world, url(world))).json()
    assert body == {"data": [], "meta": {"next_cursor": None}}


# --- latest -----------------------------------------------------------------------------


async def test_latest(world):
    latest = url(world, query="/latest")
    assert (await get(world, latest)).json() == {"data": None}  # not checked yet
    ids = await add_heartbeats(world, "api", ["up", "up", "down"])
    data = (await get(world, latest)).json()["data"]
    assert (data["id"], data["status"]) == (ids[-1], "down")


# --- who may read ------------------------------------------------------------------------


async def test_outsiders_and_unknown_monitors_get_the_same_404(world):
    await add_heartbeats(world, "api", ["up"])
    for path in (url(world), url(world, query="/latest")):
        resp = await get(world, path, who="outsider")
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "monitor_not_found")
    missing = await get(world, f"/v1/monitors/{uuid4()}/heartbeats")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "monitor_not_found")
    assert (await world["client"].get(url(world))).status_code == 401


async def test_api_keys_need_monitors_read(world):
    async def key(scopes):
        resp = await world["client"].post(
            "/v1/api-keys",
            json={"organization_id": world["org_id"], "name": f"k-{secrets.token_hex(3)}", "scopes": scopes},
            headers=bearer(world["owner"]),
        )
        return {"Authorization": f"Bearer {resp.json()['data']['key']}"}

    without = await get(world, url(world), headers=await key(["status_pages:read"]))
    assert (without.status_code, without.json()["error"]["code"]) == (403, "insufficient_scope")
    assert (await get(world, url(world), headers=await key(["monitors:read"]))).status_code == 200


async def test_deleting_the_monitor_deletes_its_history(world):
    await add_heartbeats(world, "api", ["up", "down"])
    assert (await world["client"].delete(f"/v1/monitors/{world['api']}", headers=bearer(world["owner"]))).status_code == 204
    assert (await get(world, url(world), who="owner")).status_code == 404
