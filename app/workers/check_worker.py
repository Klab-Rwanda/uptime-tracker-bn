import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.checks.result import CheckResult
from app.incidents.alerts import ensure_incident_alerts
from app.incidents.history import SqlCheckHistory
from app.incidents.service import evaluate_monitor
from app.models.heartbeat import Heartbeat
from app.models.monitor import Monitor
from app.services.notifications.delivery import DeliveryStore
from app.services.status import run_with_retry
from app.workers.heartbeat import save_heartbeat

logger = logging.getLogger(__name__)

CheckRunner = Callable[[Monitor], Awaitable[CheckResult]]

# Monitors checked at the same time. Retries wait between attempts, so checking
# one by one would let a single failing site delay every other monitor.
DEFAULT_CONCURRENCY = 10
BATCH_SIZE = 200


async def run_monitor(
    session: AsyncSession,
    monitor: Monitor,
    *,
    check: CheckRunner = run_with_retry,
    alerts: DeliveryStore | None = None,
) -> Heartbeat:
    """Check one monitor, store the heartbeat, open/resolve its incident, and
    queue the alerts for it (when `alerts` is given)."""
    monitor_id = monitor.id  # read before commits expire the object
    result = await check(monitor)
    heartbeat = await save_heartbeat(session, monitor, result)
    try:
        evaluation = await evaluate_monitor(session, monitor_id, SqlCheckHistory(session))
        if alerts is not None:
            await ensure_incident_alerts(session, alerts, evaluation)
    except Exception:
        # The heartbeat is already saved; the next check evaluates (and queues) again
        await session.rollback()
        logger.exception("incident evaluation failed", extra={"monitor_id": str(monitor_id)})
    return heartbeat


async def _due_monitor_ids(
    session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> list[UUID]:
    async with session_factory() as session:
        result = await session.scalars(
            select(Monitor.id)
            .where(
                Monitor.is_active.is_(True),
                Monitor.monitor_type.in_(["http", "https", "tcp", "dns"]),
                Monitor.next_check_at <= now,
            )
            .order_by(Monitor.next_check_at)
            .limit(BATCH_SIZE)
        )
        return list(result.all())


async def run_due_monitors(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    check: CheckRunner = run_with_retry,
    alerts: DeliveryStore | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    now: datetime | None = None,
) -> int:
    """Check every due monitor. Returns how many were checked successfully.

    Each monitor gets its own session, so one failure (a crash, a database
    error) can't leave a shared session broken for the rest of the batch.
    """
    monitor_ids = await _due_monitor_ids(session_factory, now or datetime.now(UTC))
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(monitor_id: UUID) -> bool:
        async with semaphore, session_factory() as session:
            monitor = await session.get(Monitor, monitor_id)
            if monitor is None or not monitor.is_active:
                return False  # deleted or paused since the batch was selected
            try:
                await run_monitor(session, monitor, check=check, alerts=alerts)
            # Isolate every monitor: whatever goes wrong, the others still run
            except Exception:
                await session.rollback()
                logger.exception("Failed to run monitor", extra={"monitor_id": str(monitor_id)})
                return False
            return True

    results = await asyncio.gather(*(run_one(monitor_id) for monitor_id in monitor_ids))
    return sum(results)
