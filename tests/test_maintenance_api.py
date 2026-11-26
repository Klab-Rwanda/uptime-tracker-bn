import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.incidents.service import open_incident, resolve_incident
from app.main import app
from app.maintenance.models import MaintenanceWindow, MaintenanceWindowMonitor
from app.models.monitor import Monitor
from app.util.config import get_settings
from app.util.database import get_db_session
from app.util.redis import get_redis_client
from tests.conftest import dispose_test_engine, make_test_engine
from tests.helpers.tenant import assert_tenant_isolated
from tests.test_status_pages_api import (
    FakeRedis,
    bearer,
    create_account,
    create_monitor,
    invite,
    sent_emails,
)

URL = "/v1/maintenance-windows"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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
        "owner": owner,
        "editor": editor,
        "viewer": viewer,
        "outsider": outsider,
        "api": await create_monitor(client, org_id, editor, "Customer API"),
        "web": await create_monitor(client, org_id, editor, "Website"),
        "foreign": await create_monitor(client, other_org, outsider, "Their API"),
    }


def body(world, **overrides) -> dict:
    now = datetime.now(UTC).replace(microsecond=0)
    values = {
        "organization_id": world["org_id"],
        "name": "Database upgrade",
        "starts_at": iso(now + timedelta(hours=1)),
        "ends_at": iso(now + timedelta(hours=3)),
        "monitor_ids": [world["api"]],
    }
    values.update(overrides)
    return values


async def create(world, **overrides) -> dict:
    resp = await world["client"].post(URL, json=body(world, **overrides), headers=bearer(world["editor"]))
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def add_past_window(world) -> UUID:
    """The API refuses windows that already ended, so write one directly."""
    now = datetime.now(UTC)
    async with world["sessions"]() as s:
        window = MaintenanceWindow(
            organization_id=UUID(world["org_id"]),
            name="Last week",
            starts_at=now - timedelta(days=7),
            ends_at=now - timedelta(days=7) + timedelta(hours=1),
            monitors=[MaintenanceWindowMonitor(monitor_id=UUID(world["api"]))],
        )
        s.add(window)
        await s.commit()
        return window.id


# --- create --------------------------------------------------------------------------


async def test_create_window(world):
    data = await create(world, suppress_notifications=False)
    assert data["name"] == "Database upgrade"
    assert data["state"] == "upcoming"
    assert data["monitor_ids"] == [world["api"]]
    assert data["suppress_notifications"] is False
    assert data["created_by_user_id"] is not None


async def test_suppression_is_on_by_default(world):
    assert (await create(world))["suppress_notifications"] is True


async def test_times_are_stored_in_utc(world):
    data = await create(world, starts_at="2030-01-10T03:00:00+02:00", ends_at="2030-01-10T05:00:00+02:00")
    assert data["starts_at"].startswith("2030-01-10T01:00:00")
    assert data["ends_at"].startswith("2030-01-10T03:00:00")


async def test_a_window_that_already_started_is_active(world):
    now = datetime.now(UTC)
    data = await create(world, starts_at=iso(now - timedelta(minutes=5)), ends_at=iso(now + timedelta(hours=1)))
    assert data["state"] == "active"


async def test_viewer_cannot_create_and_outsider_cannot_use_the_org(world):
    client = world["client"]
    assert (await client.post(URL, json=body(world), headers=bearer(world["viewer"]))).status_code == 403
    assert (await client.post(URL, json=body(world), headers=bearer(world["outsider"]))).status_code == 404


def _shift(hours: float) -> str:
    return iso(datetime.now(UTC) + timedelta(hours=hours))


@pytest.mark.parametrize(
    "overrides",
    [
        {"ends_at": _shift(0.5)},  # ends before it starts
        {"ends_at": _shift(1)},  # ends when it starts
        {"starts_at": "2030-01-10T01:00:00", "ends_at": "2030-01-10T03:00:00"},  # no timezone
        {"ends_at": _shift(1 + 24 * 31)},  # longer than 30 days
        {"starts_at": _shift(-5), "ends_at": _shift(-1)},  # already over
        {"monitor_ids": []},
        {"monitor_ids": ["not-a-uuid"]},
        {"name": "   "},
        {"name": "bad\x00name"},
        {"color": "red"},
    ],
)
async def test_bad_input_is_rejected(world, overrides):
    resp = await world["client"].post(URL, json=body(world, **overrides), headers=bearer(world["editor"]))
    assert resp.status_code == 422


async def test_the_same_monitor_twice_is_rejected(world):
    resp = await world["client"].post(
        URL, json=body(world, monitor_ids=[world["api"], world["api"]]), headers=bearer(world["editor"])
    )
    assert resp.status_code == 422


async def test_monitors_of_another_organization_are_refused(world):
    resp = await world["client"].post(
        URL, json=body(world, monitor_ids=[world["api"], world["foreign"]]), headers=bearer(world["editor"])
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "monitor_not_found"


# --- list ----------------------------------------------------------------------------


async def test_list_and_filter_by_state(world):
    now = datetime.now(UTC)
    upcoming = await create(world, name="Later")
    active = await create(world, name="Now", starts_at=iso(now - timedelta(minutes=5)), ends_at=iso(now + timedelta(hours=1)))
    past_id = str(await add_past_window(world))

    async def names(query=""):
        resp = await world["client"].get(
            f"{URL}?organization_id={world['org_id']}{query}", headers=bearer(world["viewer"])
        )
        assert resp.status_code == 200
        return [w["id"] for w in resp.json()["data"]]

    assert await names() == [upcoming["id"], active["id"], past_id]  # newest start first
    assert await names("&state=upcoming") == [upcoming["id"]]
    assert await names("&state=active") == [active["id"]]
    assert await names("&state=past") == [past_id]
    assert await names("&limit=1") == [upcoming["id"]]


@pytest.mark.parametrize("query", ["&state=soon", "&limit=0", "&limit=501"])
async def test_bad_list_parameters(world, query):
    resp = await world["client"].get(f"{URL}?organization_id={world['org_id']}{query}", headers=bearer(world["viewer"]))
    assert resp.status_code == 422


# --- update and delete ----------------------------------------------------------------


async def test_update_fields(world):
    window = await create(world)
    resp = await world["client"].patch(
        f"{URL}/{window['id']}",
        json={"name": "Bigger upgrade", "monitor_ids": [world["web"], world["api"]], "suppress_notifications": False},
        headers=bearer(world["editor"]),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == "Bigger upgrade"
    assert sorted(data["monitor_ids"]) == sorted([world["web"], world["api"]])
    assert data["suppress_notifications"] is False
    assert data["starts_at"] == window["starts_at"]  # not sent, not changed


async def test_moving_the_end_is_checked_against_the_stored_start(world):
    window = await create(world)
    before_start = iso(datetime.fromisoformat(window["starts_at"]) - timedelta(minutes=1))
    resp = await world["client"].patch(
        f"{URL}/{window['id']}", json={"ends_at": before_start}, headers=bearer(world["editor"])
    )
    assert resp.status_code == 422


async def test_extending_a_window(world):
    window = await create(world)
    later = iso(datetime.fromisoformat(window["ends_at"]) + timedelta(hours=2))
    resp = await world["client"].patch(f"{URL}/{window['id']}", json={"ends_at": later}, headers=bearer(world["editor"]))
    assert resp.status_code == 200
    assert resp.json()["data"]["ends_at"].startswith(later[:19])


@pytest.mark.parametrize(
    "payload",
    [{}, {"name": None}, {"starts_at": None}, {"monitor_ids": None}, {"monitor_ids": []}, {"extra": 1}],
)
async def test_bad_updates_are_rejected(world, payload):
    window = await create(world)
    resp = await world["client"].patch(f"{URL}/{window['id']}", json=payload, headers=bearer(world["editor"]))
    assert resp.status_code == 422


async def test_update_cannot_add_foreign_monitors(world):
    window = await create(world)
    resp = await world["client"].patch(
        f"{URL}/{window['id']}", json={"monitor_ids": [world["foreign"]]}, headers=bearer(world["editor"])
    )
    assert resp.status_code == 404


async def test_viewer_cannot_update_and_only_admins_delete(world):
    window = await create(world)
    client, url = world["client"], f"{URL}/{window['id']}"
    assert (await client.patch(url, json={"name": "x"}, headers=bearer(world["viewer"]))).status_code == 403
    assert (await client.delete(url, headers=bearer(world["editor"]))).status_code == 403
    assert (await client.delete(url, headers=bearer(world["owner"]))).status_code == 204
    assert (await client.delete(url, headers=bearer(world["owner"]))).status_code == 404


async def test_other_organizations_see_nothing(world):
    window = await create(world)
    outsider = bearer(world["outsider"])
    for method, url in [
        ("PATCH", f"{URL}/{window['id']}"),
        ("DELETE", f"{URL}/{window['id']}"),
        ("GET", f"{URL}?organization_id={world['org_id']}"),
    ]:
        await assert_tenant_isolated(
            world["client"], method, url, outsider_headers=outsider, json={"name": "x"} if method == "PATCH" else None
        )


async def test_deleting_a_monitor_removes_it_from_windows(world):
    window = await create(world, monitor_ids=[world["api"], world["web"]])
    await world["client"].delete(f"/v1/monitors/{world['web']}", headers=bearer(world["owner"]))
    resp = await world["client"].get(f"{URL}?organization_id={world['org_id']}", headers=bearer(world["viewer"]))
    [data] = [w for w in resp.json()["data"] if w["id"] == window["id"]]
    assert data["monitor_ids"] == [world["api"]]


async def test_api_keys_need_maintenance_scopes(world):
    async def key(scopes):
        resp = await world["client"].post(
            "/v1/api-keys",
            json={"organization_id": world["org_id"], "name": f"k-{secrets.token_hex(3)}", "scopes": scopes},
            headers=bearer(world["owner"]),
        )
        return {"Authorization": f"Bearer {resp.json()['data']['key']}"}

    client, list_url = world["client"], f"{URL}?organization_id={world['org_id']}"
    assert (await client.get(list_url, headers=await key(["monitors:read"]))).status_code == 403
    assert (await client.get(list_url, headers=await key(["maintenance:read"]))).status_code == 200
    assert (await client.post(URL, json=body(world), headers=await key(["maintenance:read"]))).status_code == 403
    assert (await client.post(URL, json=body(world), headers=await key(["maintenance:write"]))).status_code == 201


# --- the public status page during maintenance ------------------------------------------


async def make_status_page(world) -> str:
    slug = f"acme-{world['tag']}"
    resp = await world["client"].post(
        "/v1/status-pages",
        json={
            "organization_id": world["org_id"],
            "title": "Acme Status",
            "slug": slug,
            "groups": [{"name": "Services", "monitor_ids": [world["api"], world["web"]]}],
        },
        headers=bearer(world["editor"]),
    )
    assert resp.status_code == 201, resp.text
    return slug


async def set_status(world, key, status, *, active=True):
    async with world["sessions"]() as s:
        monitor = await s.get(Monitor, UUID(world[key]))
        monitor.current_status, monitor.is_active = status, active
        await s.commit()


async def test_status_page_shows_maintenance_and_hides_its_incident(world):
    slug = await make_status_page(world)
    await set_status(world, "api", "down")
    await set_status(world, "web", "up")
    now = datetime.now(UTC)
    async with world["sessions"]() as s:
        monitor = await s.get(Monitor, UUID(world["api"]))
        await open_incident(s, monitor, started_at=now - timedelta(minutes=2), now=now)
    await create(world, starts_at=iso(now - timedelta(minutes=5)), ends_at=iso(now + timedelta(hours=1)))

    data = (await world["client"].get(f"/status/{slug}")).json()["data"]
    assert data["groups"][0]["monitors"] == [
        {"name": "Customer API", "status": "maintenance"},
        {"name": "Website", "status": "up"},
    ]
    assert data["overall_status"] == "maintenance"
    assert data["active_incidents"] == []


async def test_upcoming_maintenance_changes_nothing_yet(world):
    slug = await make_status_page(world)
    await set_status(world, "api", "down")
    await set_status(world, "web", "up")
    await create(world)  # starts in an hour

    data = (await world["client"].get(f"/status/{slug}")).json()["data"]
    assert data["groups"][0]["monitors"][0] == {"name": "Customer API", "status": "down"}
    assert data["overall_status"] == "partial_outage"


async def test_a_paused_monitor_stays_paused_during_maintenance(world):
    slug = await make_status_page(world)
    await set_status(world, "api", "up", active=False)
    now = datetime.now(UTC)
    await create(world, starts_at=iso(now - timedelta(minutes=5)), ends_at=iso(now + timedelta(hours=1)))
    data = (await world["client"].get(f"/status/{slug}")).json()["data"]
    assert data["groups"][0]["monitors"][0]["status"] == "paused"


# --- maintenance calendar and incident history on the public page ----------------------


async def add_window(world, name, starts_at, ends_at, keys) -> None:
    async with world["sessions"]() as s:
        s.add(
            MaintenanceWindow(
                organization_id=UUID(world["org_id"]),
                name=name,
                starts_at=starts_at,
                ends_at=ends_at,
                monitors=[MaintenanceWindowMonitor(monitor_id=UUID(world[k])) for k in keys],
            )
        )
        await s.commit()


async def add_incident(world, key, started_at, resolved_at=None) -> None:
    async with world["sessions"]() as s:
        monitor = await s.get(Monitor, UUID(world[key]))
        incident, _ = await open_incident(s, monitor, started_at=started_at, cause="secret 10.0.0.5", now=started_at)
        if resolved_at is not None:
            await resolve_incident(s, incident.id, resolved_at=resolved_at)


async def public_page(world, slug) -> dict:
    resp = await world["client"].get(f"/status/{slug}")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def test_calendar_shows_current_and_coming_maintenance(world):
    slug = await make_status_page(world)
    world["db"] = await create_monitor(world["client"], world["org_id"], world["editor"], "Database")  # not on the page
    now = datetime.now(UTC).replace(microsecond=0)
    await add_window(world, "Now", now - timedelta(minutes=5), now + timedelta(hours=1), ["api"])
    await add_window(world, "Tomorrow", now + timedelta(days=1), now + timedelta(days=1, hours=2), ["web", "api", "db"])
    await add_window(world, "Too far", now + timedelta(days=20), now + timedelta(days=20, hours=1), ["api"])
    await add_window(world, "Over", now - timedelta(days=2), now - timedelta(days=2) + timedelta(hours=1), ["api"])
    await add_window(world, "Not on page", now + timedelta(hours=2), now + timedelta(hours=3), ["db"])

    calendar = (await public_page(world, slug))["maintenance"]
    assert [(m["name"], m["status"], m["monitors"]) for m in calendar] == [
        ("Now", "in_progress", ["Customer API"]),
        ("Tomorrow", "scheduled", ["Customer API", "Website"]),  # page order; Database is not on the page
    ]
    assert calendar[1]["starts_at"].startswith(iso(now + timedelta(days=1))[:19])


async def test_history_lists_resolved_incidents_with_their_duration(world):
    slug = await make_status_page(world)
    now = datetime.now(UTC).replace(microsecond=0)
    await add_incident(world, "api", now - timedelta(days=3), now - timedelta(days=3) + timedelta(minutes=12))
    await add_incident(world, "web", now - timedelta(days=1), now - timedelta(days=1) + timedelta(seconds=90))
    await add_incident(world, "api", now - timedelta(days=100), now - timedelta(days=100) + timedelta(hours=1))  # too old
    await add_incident(world, "web", now - timedelta(minutes=10))  # still open: not history

    data = await public_page(world, slug)
    assert [(i["title"], i["duration_seconds"]) for i in data["incident_history"]] == [
        ("Website is down", 90),
        ("Customer API is down", 12 * 60),
    ]
    assert [i["title"] for i in data["active_incidents"]] == ["Website is down"]
    assert "10.0.0.5" not in str(data)  # the cause never reaches the public


async def test_history_leaves_out_incidents_during_maintenance(world):
    slug = await make_status_page(world)
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=5)
    await add_window(world, "Planned work", start, start + timedelta(hours=2), ["api"])
    await add_incident(world, "api", start + timedelta(minutes=10), start + timedelta(minutes=40))  # during the window
    await add_incident(world, "web", start + timedelta(minutes=10), start + timedelta(minutes=40))  # other monitor
    await add_incident(world, "api", start + timedelta(hours=3), start + timedelta(hours=4))  # after it ended

    history = (await public_page(world, slug))["incident_history"]
    assert sorted((i["title"], i["started_at"][:16]) for i in history) == sorted([
        ("Website is down", iso(start + timedelta(minutes=10))[:16]),
        ("Customer API is down", iso(start + timedelta(hours=3))[:16]),
    ])


async def test_an_empty_page_has_no_calendar_or_history(world):
    resp = await world["client"].post(
        "/v1/status-pages",
        json={"organization_id": world["org_id"], "title": "Empty", "slug": f"empty-{world['tag']}", "groups": []},
        headers=bearer(world["editor"]),
    )
    assert resp.status_code == 201
    data = await public_page(world, f"empty-{world['tag']}")
    assert (data["maintenance"], data["incident_history"]) == ([], [])
