from datetime import UTC
,datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.checks.result import CheckResult
from app.models.heartbeat import Heartbeat
from app.models.monitor import Monitor


async def save_heartbeat(
    session: AsyncSession,
    monitor: Monitor,
    result: CheckResult,
) -> Heartbeat:
    heartbeat = Heartbeat(
        id=uuid4(),
        monitor_id=monitor.id,
        status=result.status,
        response_time_ms=result.response_time_ms,
        checked_at=result.checked_at,
        error_message=result.error_message,
    )
    session.add(heartbeat)

    is_up = result.status == "up"

    if is_up:
        monitor.consecutive_failures = 0
        monitor.consecutive_successes += 1
        monitor.current_status = "up"
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0
        monitor.current_status = "down"

    now = datetime.now(UTC)
    monitor.last_checked_at = now
    monitor.next_check_at = now + timedelta(seconds=monitor.check_interval_seconds)

    await session.commit()
    await session.refresh(heartbeat)
    return heartbeat
