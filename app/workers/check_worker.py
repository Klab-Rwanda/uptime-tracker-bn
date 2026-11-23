import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.checks.http_check import HTTPCheck
from app.models.heartbeat import Heartbeat
from app.models.monitor import Monitor


def parse_status_codes(raw: str | None) -> list[int]:
    if not raw:
        return [200]
    return [int(code.strip()) for code in raw.split(",") if code.strip().isdigit()]


async def run_monitor(monitor: Monitor, db: AsyncSession) -> None:
    check = HTTPCheck(
        monitor_id=str(monitor.id),
        target=monitor.target,
        method=monitor.method or "GET",
        expected_status_codes=parse_status_codes(monitor.expected_status_codes),
        keyword=monitor.keyword,
        timeout_seconds=monitor.timeout_seconds,
    )

    result = await check.run()

    heartbeat = Heartbeat(
        id=uuid.uuid4(),
        monitor_id=monitor.id,
        status=result.status,
        response_time_ms=result.response_time_ms,
        checked_at=result.checked_at,
        error_message=result.error_message,
    )
    db.add(heartbeat)

    monitor.current_status = result.status
    monitor.last_checked_at = result.checked_at
    monitor.next_check_at = datetime.fromtimestamp(
        result.checked_at.timestamp() + monitor.check_interval_seconds,
        tz=timezone.utc,
    )

    if result.status == "up":
        monitor.consecutive_successes += 1
        monitor.consecutive_failures = 0
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0

    await db.commit()


async def run_due_monitors(db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)

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
        except Exception as e:
            print(f"Failed to run monitor {monitor.id}: {e}")
