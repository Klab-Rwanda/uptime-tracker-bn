import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.auth.models import User
from app.incidents.history import InMemoryCheckHistory
from app.incidents.service import (
    IncidentNotFound,
    acknowledge_incident,
    add_note,
    evaluate_monitor,
    get_active_incident,
    get_timeline,
    open_incident,
    resolve_incident,
)
from app.models.incident import Incident, IncidentEvent
from app.models.monitor import Monitor
from app.organizations.models import Organization
from app.services.incident_logic import InvalidIncidentTransition
from app.services.incident_rules import CheckResult, IncidentAction
from tests.conftest import dispose_test_engine, make_test_engine

T0 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
NONE, OPEN, RESOLVE = IncidentAction.NONE, IncidentAction.OPEN, IncidentAction.RESOLVE


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def aware(value: datetime | None) -> datetime | None:
    # SQLite gives timestamps back without a timezone; everything stored is UTC
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def engine(request, tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = await make_test_engine(request.param, tmp_path)  # every table
    yield engine
    await dispose_test_engine(engine)


@pytest.fixture
def sessions(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def world(sessions) -> dict:
    """An organization with one monitor (60 s interval), a second org, and a user."""
    async with sessions() as s:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        other_org = Organization(name="Other", slug=f"other-{uuid4().hex[:6]}")
        user = User(email=f"u{uuid4().hex[:6]}@example.com", password_hash="x", full_name="U")
        s.add_all([org, other_org, user])
        await s.flush()
        monitor = Monitor(
            organization_id=org.id,
            name="Customer API",
            monitor_type="http",
            target="https://api.example.com/health",
            check_interval_seconds=60,
            retry_count=0,
            retry_interval_seconds=30,
            next_check_at=T0,
        )
        s.add(monitor)
        await s.commit()
        return {"org": org.id, "other_org": other_org.id, "user": user.id, "monitor": monitor.id}


@pytest.fixture
def history() -> InMemoryCheckHistory:
    return InMemoryCheckHistory()


def record(history, world, *statuses: str, start: float = 0, message: str | None = None):
    for i, status in enumerate(statuses):
        history.add(
            world["monitor"],
            CheckResult(status=status, checked_at=at(start + i), message=message),
        )


async def evaluate(sessions, world, history):
    # Real clock: each evaluation happens at its own moment, like in production,
    # so timeline events never share a timestamp. Incident start/recovery times
    # come from the check results, not from this clock.
    async with sessions() as s:
        return await evaluate_monitor(s, world["monitor"], history)


async def count_incidents(sessions) -> int:
    async with sessions() as s:
        return await s.scalar(select(func.count()).select_from(Incident))


async def timeline(sessions, incident_id) -> list[str]:
    async with sessions() as s:
        return [e.event_type for e in await get_timeline(s, incident_id)]


# --- automatic detection ------------------------------------------------------


async def test_two_failures_open_an_incident_dated_from_the_first(sessions, world, history):
    record(history, world, "up", "down", "down", message="HTTP 503 from API")
    result = await evaluate(sessions, world, history)

    assert result.action is OPEN
    incident = result.incident
    assert incident.status == "open"
    assert incident.title == "Customer API is down"
    assert incident.cause == "HTTP 503 from API"
    assert aware(incident.started_at) == at(1)  # the first failure, not detection time
    assert incident.organization_id == world["org"]  # always the monitor's tenant
    assert await timeline(sessions, incident.id) == ["opened"]


async def test_one_failure_does_nothing(sessions, world, history):
    record(history, world, "up", "down")
    assert (await evaluate(sessions, world, history)).action is NONE
    assert await count_incidents(sessions) == 0


async def test_still_failing_never_opens_a_second_incident(sessions, world, history):
    record(history, world, "down", "down")
    first = await evaluate(sessions, world, history)
    record(history, world, "down", "down", start=2)
    again = await evaluate(sessions, world, history)

    assert again.action is NONE
    assert again.incident.id == first.incident.id
    assert await count_incidents(sessions) == 1


async def test_recovery_resolves_at_the_first_success(sessions, world, history):
    record(history, world, "down", "down")
    opened = (await evaluate(sessions, world, history)).incident
    record(history, world, "up", start=2)
    result = await evaluate(sessions, world, history)

    assert result.action is RESOLVE
    assert result.incident.id == opened.id
    assert result.incident.status == "resolved"
    assert aware(result.incident.resolved_at) == at(2)
    assert await timeline(sessions, opened.id) == ["opened", "resolved"]


async def test_a_new_outage_after_recovery_is_a_new_incident(sessions, world, history):
    record(history, world, "down", "down")
    await evaluate(sessions, world, history)
    record(history, world, "up", start=2)
    await evaluate(sessions, world, history)
    record(history, world, "down", "down", start=3)

    assert (await evaluate(sessions, world, history)).action is OPEN
    assert await count_incidents(sessions) == 2


async def test_failures_far_apart_do_not_combine(sessions, world, history):
    # 60 s monitor: the rule allows 3 minutes between results of one streak
    history.add(world["monitor"], CheckResult(status="down", checked_at=at(0)))
    history.add(world["monitor"], CheckResult(status="down", checked_at=at(60)))
    assert (await evaluate(sessions, world, history)).action is NONE


async def test_paused_monitor_neither_opens_nor_resolves(sessions, world, history):
    async with sessions() as s:
        monitor = await s.get(Monitor, world["monitor"])
        monitor.is_active = False
        await s.commit()
    record(history, world, "down", "down")
    result = await evaluate(sessions, world, history)
    assert result.action is NONE
    assert result.reason == "monitor is paused"


async def test_unknown_monitor_does_nothing(sessions, history):
    async with sessions() as s:
        result = await evaluate_monitor(s, uuid4(), history)
    assert result.action is NONE


async def test_parallel_workers_open_exactly_one_incident(sessions, world, history):
    record(history, world, "down", "down")
    results = await asyncio.gather(*(evaluate(sessions, world, history) for _ in range(5)))

    assert [r.action for r in results].count(OPEN) == 1
    assert len({r.incident.id for r in results}) == 1
    assert await count_incidents(sessions) == 1


async def test_auto_resolve_works_on_an_acknowledged_incident(sessions, world, history):
    record(history, world, "down", "down")
    opened = (await evaluate(sessions, world, history)).incident
    async with sessions() as s:
        await acknowledge_incident(s, opened.id, actor_user_id=world["user"], now=at(5))
    record(history, world, "up", start=6)
    assert (await evaluate(sessions, world, history)).action is RESOLVE


# --- opening directly -----------------------------------------------------------


async def test_open_incident_is_idempotent(sessions, world):
    async with sessions() as s:
        monitor = await s.get(Monitor, world["monitor"])
        first, created_first = await open_incident(s, monitor, started_at=at(0))
    async with sessions() as s:
        monitor = await s.get(Monitor, world["monitor"])
        second, created_second = await open_incident(s, monitor, started_at=at(1))
    assert (created_first, created_second) == (True, False)
    assert first.id == second.id


async def test_naive_start_time_is_rejected(sessions, world):
    async with sessions() as s:
        monitor = await s.get(Monitor, world["monitor"])
        with pytest.raises(ValueError):
            await open_incident(s, monitor, started_at=datetime(2026, 11, 25, 10, 0))


async def open_now(sessions, world, started: float = 0) -> Incident:
    async with sessions() as s:
        monitor = await s.get(Monitor, world["monitor"])
        incident, _ = await open_incident(s, monitor, started_at=at(started), now=at(started))
        return incident


# --- acknowledge ------------------------------------------------------------------


async def test_acknowledge_records_who_and_when(sessions, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        acked = await acknowledge_incident(s, incident.id, actor_user_id=world["user"], now=at(3))
    assert acked.status == "acknowledged"
    assert aware(acked.acknowledged_at) == at(3)
    async with sessions() as s:
        event = (await get_timeline(s, incident.id))[-1]
    assert (event.event_type, event.actor_user_id) == ("acknowledged", world["user"])


async def test_cannot_acknowledge_twice_or_after_resolving(sessions, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        await acknowledge_incident(s, incident.id, actor_user_id=world["user"], now=at(1))
    async with sessions() as s:
        with pytest.raises(InvalidIncidentTransition):
            await acknowledge_incident(s, incident.id, actor_user_id=world["user"], now=at(2))

    other = await open_now(sessions, world, started=10)  # previous one is still active
    assert other.id == incident.id
    async with sessions() as s:
        await resolve_incident(s, incident.id, actor_user_id=world["user"], now=at(4))
    async with sessions() as s:
        with pytest.raises(InvalidIncidentTransition):
            await acknowledge_incident(s, incident.id, actor_user_id=world["user"], now=at(5))


# --- resolve --------------------------------------------------------------------------


async def test_manual_resolve_records_the_person(sessions, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        resolved = await resolve_incident(
            s, incident.id, actor_user_id=world["user"], message="Fixed the DB", now=at(7)
        )
    assert resolved.status == "resolved"
    assert aware(resolved.resolved_at) == at(7)
    async with sessions() as s:
        event = (await get_timeline(s, incident.id))[-1]
    assert (event.event_type, event.message, event.actor_user_id) == (
        "resolved",
        "Fixed the DB",
        world["user"],
    )


async def test_resolve_time_is_never_before_the_start(sessions, world):
    incident = await open_now(sessions, world, started=10)
    async with sessions() as s:
        resolved = await resolve_incident(s, incident.id, resolved_at=at(5), now=at(12))
    assert aware(resolved.resolved_at) == at(10)


async def test_cannot_resolve_twice(sessions, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        await resolve_incident(s, incident.id, now=at(1))
    async with sessions() as s:
        with pytest.raises(InvalidIncidentTransition):
            await resolve_incident(s, incident.id, now=at(2))


async def test_parallel_resolves_succeed_exactly_once(sessions, world):
    incident = await open_now(sessions, world)

    async def try_resolve():
        async with sessions() as s:
            try:
                await resolve_incident(s, incident.id, now=at(1))
                return "resolved"
            except InvalidIncidentTransition:
                return "refused"

    outcomes = await asyncio.gather(*(try_resolve() for _ in range(5)))
    assert sorted(outcomes) == ["refused"] * 4 + ["resolved"]
    assert (await timeline(sessions, incident.id)).count("resolved") == 1


async def test_unknown_incident_raises(sessions, world):
    async with sessions() as s:
        with pytest.raises(IncidentNotFound):
            await resolve_incident(s, uuid4())


# --- notes and timeline ----------------------------------------------------------------


async def test_full_timeline_in_order(sessions, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        await acknowledge_incident(s, incident.id, actor_user_id=world["user"], now=at(1))
    async with sessions() as s:
        await add_note(s, incident.id, actor_user_id=world["user"], message="DB failover", now=at(2))
    async with sessions() as s:
        await resolve_incident(s, incident.id, now=at(3))
    async with sessions() as s:
        await add_note(s, incident.id, actor_user_id=world["user"], message="Post-mortem", now=at(4))

    assert await timeline(sessions, incident.id) == [
        "opened",
        "acknowledged",
        "note",
        "resolved",
        "note",
    ]


@pytest.mark.parametrize("message", ["", "   ", "x" * 5001])
async def test_bad_notes_are_rejected(sessions, world, message):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        with pytest.raises(ValueError):
            await add_note(s, incident.id, actor_user_id=world["user"], message=message)


async def test_note_is_trimmed(sessions, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        event = await add_note(s, incident.id, actor_user_id=world["user"], message="  hi  ")
    assert event.message == "hi"


async def test_note_on_unknown_incident_raises(sessions, world):
    async with sessions() as s:
        with pytest.raises(IncidentNotFound):
            await add_note(s, uuid4(), actor_user_id=world["user"], message="x")


# --- database rules ------------------------------------------------------------------------


async def test_deleting_a_monitor_deletes_its_incidents_and_timeline(sessions, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        await s.execute(delete(Monitor).where(Monitor.id == world["monitor"]))
        await s.commit()
    async with sessions() as s:
        assert await s.get(Incident, incident.id) is None
        assert await s.scalar(select(func.count()).select_from(IncidentEvent)) == 0


async def test_active_incident_lookup(sessions, world):
    async with sessions() as s:
        assert await get_active_incident(s, world["monitor"]) is None
    incident = await open_now(sessions, world)
    async with sessions() as s:
        assert (await get_active_incident(s, world["monitor"])).id == incident.id
        await resolve_incident(s, incident.id, now=at(1))
    async with sessions() as s:
        assert await get_active_incident(s, world["monitor"]) is None
