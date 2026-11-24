import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monitor import Monitor
from app.services.status import resolve_status, run_with_retry
from app.workers.heartbeat import save_heartbeat

logger = logging.getLogger(__name__)


async def run_monitor(monitor: Monitor, db: AsyncSession) -> None:
    result = await run_with_retry(monitor)

    monitor.current_status = resolve_status(monitor, result)
    monitor.last_checked_at = result.checked_at
    monitor.next_check_at = datetime.now(UTC) + timedelta(seconds=monitor.check_interval_seconds)

    if result.status == "up":
        monitor.consecutive_successes += 1
        monitor.consecutive_failures = 0
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0

    await save_heartbeat(db, monitor, result)


async def run_due_monitors(db: AsyncSession) -> None:
    now = datetime.now(UTC)

    result = await db.execute(
        select(Monitor).where(
            Monitor.is_active == True,
            Monitor.monitor_type == "http",
            Monitor.next_check_at <= now,
        )
    )
    monitors = result.scalars().all()

    for monitor in monitors:
        try:
            await run_monitor(monitor, db)
        except Exception:
            logger.exception("Failed to run monitor %s", monitor.id)
