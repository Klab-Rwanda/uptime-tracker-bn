from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.checks.result import CheckResult
from app.models.heartbeat import Heartbeat
from app.models.monitor import Monitor
from app.services.status import resolve_status


async def save_heartbeat(
    session: AsyncSession,
    monitor: Monitor,
    result: CheckResult,
    *,
    now: datetime | None = None,
) -> Heartbeat:
    """Store one check result and update the monitor's state, once.

    This is the ONLY place the counters and status are updated (they used to be
    updated here and in run_monitor, so every count was doubled).
    """
    heartbeat = Heartbeat(
        monitor_id=monitor.id,
        status=result.status,
        response_time_ms=result.response_time_ms,
        checked_at=result.checked_at,
        error_message=result.error_message,
    )
    session.add(heartbeat)

    # Uses the counters from BEFORE this result, so it must run first
    monitor.current_status = resolve_status(monitor, result)
    if result.status == "up":
        monitor.consecutive_failures = 0
        monitor.consecutive_successes += 1
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0

    now = now or datetime.now(UTC)
    monitor.last_checked_at = result.checked_at
    monitor.next_check_at = now + timedelta(seconds=monitor.check_interval_seconds)

    await session.commit()
    await session.refresh(heartbeat)
    return heartbeat
