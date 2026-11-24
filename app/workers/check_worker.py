import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.checks.dns_check import DNSCheck
from app.checks.http_check import HTTPCheck
from app.checks.tcp_check import TCPCheck
from app.models.monitor import Monitor
from app.services.incident_logic import should_open_incident, should_resolve_incident
from app.workers.heartbeat import save_heartbeat

logger = logging.getLogger(__name__)


def _build_check(monitor: Monitor):
    monitor_id = str(monitor.id)
    if monitor.monitor_type in ("http", "https"):
        codes = json.loads(monitor.expected_status_codes) if monitor.expected_status_codes else None
        return HTTPCheck(
            monitor_id=monitor_id,
            target=monitor.target,
            method=monitor.method or "GET",
            expected_status_codes=codes,
            keyword=monitor.keyword,
            timeout_seconds=monitor.timeout_seconds,
        )
    if monitor.monitor_type == "tcp":
        return TCPCheck(
            monitor_id=monitor_id,
            target=monitor.target,
            timeout_seconds=monitor.timeout_seconds,
        )
    if monitor.monitor_type == "dns":
        return DNSCheck(
            monitor_id=monitor_id,
            target=monitor.target,
            timeout_seconds=monitor.timeout_seconds,
        )
    raise ValueError(f"Unsupported monitor type: {monitor.monitor_type}")


async def run_check(monitor: Monitor, session: AsyncSession) -> None:
    has_active_incident = (
        monitor.consecutive_failures >= 2 and monitor.current_status == "down"
    )

    try:
        check = _build_check(monitor)
        result = await check.run()
    except Exception:
        logger.exception("Check failed for monitor %s", monitor.id)
        return

    await save_heartbeat(session, monitor, result)

    is_up = result.status == "up"

    if should_open_incident(monitor.consecutive_failures, has_active_incident):
        logger.info("Incident should be opened for monitor %s", monitor.id)

    if should_resolve_incident(is_up, has_active_incident):
        logger.info("Incident should be resolved for monitor %s", monitor.id)


async def run_due_monitors(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Monitor).where(
            Monitor.is_active == True,
            Monitor.next_check_at <= now,
        )
    )
    monitors = result.scalars().all()

    for monitor in monitors:
        try:
            await run_check(monitor, session)
        except Exception:
            logger.exception("Unexpected error running monitor %s", monitor.id)
