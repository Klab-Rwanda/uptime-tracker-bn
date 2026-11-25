from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.checks.result import CheckResult
from app.incidents.history import SqlCheckHistory
from app.models.heartbeat import Heartbeat
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.organizations.models import Organization
from app.workers import check_worker
from app.workers.check_worker import run_due_monitors
from app.workers.heartbeat import save_heartbeat
from tests.conftest import dispose_test_engine, make_test_engine

T0 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def engine(request, tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = await make_test_engine(request.param, tmp_path)
    yield engine
    await dispose_test_engine(engine)


@pytest.fixture
def sessions(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def org_id(sessions):
    async with sessions() as s:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        s.add(org)
        await s.commit()
        return org.id


async def add_monitor(sessions, org_id, **overrides):
    values = {
        "organization_id": org_id,
        "name": f"Monitor {uuid4().hex[:4]}",
        "monitor_type": "http",
        "target": "https://status.acme.com/health",
        "check_interval_seconds": 60,
        "retry_count": 0,
        "next_check_at": T0 - timedelta(seconds=1),
    }
    values.update(overrides)
    async with sessions() as s:
        monitor = Monitor(**values)
        s.add(monitor)
        await s.commit()
        return monitor.id


class ScriptedChecks:
    """Returns the next scripted status for each monitor, one minute apart."""

    def __init__(self, script: dict, *, crash_for=()):
        self.script = {k: list(v) for k, v in script.items()}
        self.crash_for = set(crash_for)
        self.clock = T0

    async def __call__(self, monitor: Monitor) -> CheckResult:
        if monitor.id in self.crash_for:
            raise RuntimeError("check crashed")
        state = self.script[monitor.id].pop(0)
        self.clock += timedelta(minutes=1)
        return CheckResult(
            monitor_id=str(monitor.id),
            status=state,
            response_time_ms=10,
            checked_at=self.clock,
            error_message=None if state == "up" else "Unexpected status code: 503",
        )


async def make_due(sessions, *monitor_ids):
    async with sessions() as s:
        for monitor_id in monitor_ids:
            (await s.get(Monitor, monitor_id)).next_check_at = T0 - timedelta(seconds=1)
        await s.commit()


async def run_round(sessions, checks, *monitor_ids):
    await make_due(sessions, *monitor_ids)
    return await run_due_monitors(sessions, check=checks, now=T0)


async def count(sessions, model, **where):
    async with sessions() as s:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return await s.scalar(stmt)


# --- heartbeats and counters ----------------------------------------------------------


async def test_heartbeat_is_saved_and_counters_move_once(sessions, org_id):
    monitor_id = await add_monitor(sessions, org_id)
    async with sessions() as s:
        monitor = await s.get(Monitor, monitor_id)
        result = CheckResult(monitor_id=str(monitor_id), status="down", response_time_ms=5, checked_at=T0)
        heartbeat = await save_heartbeat(s, monitor, result, now=T0)

    async with sessions() as s:
        monitor = await s.get(Monitor, monitor_id)
        assert (monitor.consecutive_failures, monitor.consecutive_successes) == (1, 0)
        assert monitor.current_status == "down"
        assert heartbeat.id is not None
    assert await count(sessions, Heartbeat, monitor_id=monitor_id) == 1


async def test_counters_after_several_checks(sessions, org_id):
    monitor_id = await add_monitor(sessions, org_id)
    checks = ScriptedChecks({monitor_id: ["down", "down", "down", "up"]})
    for _ in range(3):
        await run_round(sessions, checks, monitor_id)
    async with sessions() as s:
        monitor = await s.get(Monitor, monitor_id)
        assert monitor.consecutive_failures == 3  # not 6: counted once per check
    await run_round(sessions, checks, monitor_id)
    async with sessions() as s:
        monitor = await s.get(Monitor, monitor_id)
        assert (monitor.consecutive_failures, monitor.consecutive_successes) == (0, 1)
        assert monitor.current_status == "up"


async def test_next_check_is_scheduled(sessions, org_id):
    monitor_id = await add_monitor(sessions, org_id, check_interval_seconds=120)
    await run_round(sessions, ScriptedChecks({monitor_id: ["up"]}), monitor_id)
    async with sessions() as s:
        monitor = await s.get(Monitor, monitor_id)
        next_check = monitor.next_check_at.replace(tzinfo=UTC) if monitor.next_check_at.tzinfo is None else monitor.next_check_at
        assert next_check > datetime.now(UTC) + timedelta(seconds=100)


# --- incidents open and resolve automatically -------------------------------------------


async def test_two_failed_checks_open_an_incident_and_recovery_resolves_it(sessions, org_id):
    monitor_id = await add_monitor(sessions, org_id)
    checks = ScriptedChecks({monitor_id: ["down", "down", "up"]})

    await run_round(sessions, checks, monitor_id)
    assert await count(sessions, Incident) == 0  # one failure is not an incident

    await run_round(sessions, checks, monitor_id)
    async with sessions() as s:
        incident = await s.scalar(select(Incident))
        assert incident.status == "open"
        assert incident.cause == "Unexpected status code: 503"
        started = incident.started_at.replace(tzinfo=UTC) if incident.started_at.tzinfo is None else incident.started_at
        assert started == T0 + timedelta(minutes=1)  # the first failed check

    await run_round(sessions, checks, monitor_id)
    async with sessions() as s:
        incident = await s.scalar(select(Incident))
        assert incident.status == "resolved"


async def test_a_failing_incident_evaluation_keeps_the_heartbeat(sessions, org_id, monkeypatch):
    monitor_id = await add_monitor(sessions, org_id)

    async def broken(*args, **kwargs):
        raise RuntimeError("incident service down")

    monkeypatch.setattr(check_worker, "evaluate_monitor", broken)
    checked = await run_round(sessions, ScriptedChecks({monitor_id: ["down"]}), monitor_id)
    assert checked == 1
    assert await count(sessions, Heartbeat, monitor_id=monitor_id) == 1


# --- which monitors run, and isolation ----------------------------------------------------


async def test_only_due_active_http_monitors_run(sessions, org_id):
    due = await add_monitor(sessions, org_id)
    await add_monitor(sessions, org_id, next_check_at=T0 + timedelta(minutes=5))  # not due
    await add_monitor(sessions, org_id, is_active=False)  # paused
    await add_monitor(sessions, org_id, monitor_type="dns", target="acme.com")  # not wired yet

    checked = await run_due_monitors(sessions, check=ScriptedChecks({due: ["up"]}), now=T0)
    assert checked == 1
    assert await count(sessions, Heartbeat) == 1


async def test_one_crashing_monitor_does_not_stop_the_others(sessions, org_id):
    ok_a = await add_monitor(sessions, org_id)
    broken = await add_monitor(sessions, org_id)
    ok_b = await add_monitor(sessions, org_id)
    checks = ScriptedChecks({ok_a: ["up"], ok_b: ["up"]}, crash_for={broken})

    checked = await run_due_monitors(sessions, check=checks, now=T0)
    assert checked == 2
    assert await count(sessions, Heartbeat, monitor_id=ok_a) == 1
    assert await count(sessions, Heartbeat, monitor_id=ok_b) == 1
    assert await count(sessions, Heartbeat, monitor_id=broken) == 0


async def test_many_monitors_run_concurrently(sessions, org_id):
    ids = [await add_monitor(sessions, org_id) for _ in range(12)]
    checks = ScriptedChecks({monitor_id: ["up"] for monitor_id in ids})
    assert await run_due_monitors(sessions, check=checks, concurrency=4, now=T0) == 12


# --- reading history for incident detection ------------------------------------------------


async def test_sql_history_returns_the_newest_results(sessions, org_id):
    monitor_id = await add_monitor(sessions, org_id)
    other_id = await add_monitor(sessions, org_id)
    async with sessions() as s:
        for minute, state in enumerate(["up", "down", "down"]):
            s.add(Heartbeat(monitor_id=monitor_id, status=state, checked_at=T0 + timedelta(minutes=minute), error_message="boom" if state == "down" else None))
        s.add(Heartbeat(monitor_id=other_id, status="up", checked_at=T0))
        await s.commit()

    async with sessions() as s:
        results = await SqlCheckHistory(s).recent_results(monitor_id, limit=2)
    assert [r.status for r in results] == ["down", "down"]
    assert results[0].checked_at == T0 + timedelta(minutes=2)
    assert results[0].checked_at.tzinfo is not None
    assert results[0].message == "boom"


async def test_heartbeat_status_is_validated(sessions, org_id):
    from sqlalchemy.exc import IntegrityError

    monitor_id = await add_monitor(sessions, org_id)
    async with sessions() as s:
        s.add(Heartbeat(monitor_id=monitor_id, status="exploded", checked_at=T0))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_deleting_a_monitor_deletes_its_heartbeats(sessions, org_id):
    from sqlalchemy import delete

    monitor_id = await add_monitor(sessions, org_id)
    async with sessions() as s:
        s.add(Heartbeat(monitor_id=monitor_id, status="up", checked_at=T0))
        await s.commit()
        await s.execute(delete(Monitor).where(Monitor.id == monitor_id))
        await s.commit()
    assert await count(sessions, Heartbeat) == 0
