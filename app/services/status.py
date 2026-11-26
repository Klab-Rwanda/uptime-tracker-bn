import asyncio
import json
from collections.abc import Awaitable, Callable

from app.checks.base import BaseCheck
from app.checks.dns_check import DNSCheck
from app.checks.http_check import HTTPCheck
from app.checks.result import CheckResult
from app.checks.tcp_check import TCPCheck
from app.models.monitor import Monitor
from app.util.config import get_settings

Sleep = Callable[[float], Awaitable[None]]


async def run_with_retry(monitor: Monitor, *, sleep: Sleep = asyncio.sleep) -> CheckResult:
    """Run the monitor's check, retrying a failure up to retry_count times.

    Waits retry_interval_seconds between attempts, so a struggling site isn't
    hit several times in a row. Only the final result is returned (and stored).
    """
    check = _build_check(monitor)

    result = await check.run()
    for _ in range(max(monitor.retry_count, 0)):
        if result.status == "up":
            break
        await sleep(max(monitor.retry_interval_seconds, 0))
        result = await check.run()
    return result


def _build_check(monitor: Monitor) -> BaseCheck:
    """Pick the right check class for this monitor type."""
    monitor_id = str(monitor.id)
    t = monitor.monitor_type

    if t in ("http", "https"):
        return HTTPCheck(
            monitor_id=monitor_id,
            target=monitor.target,
            method=monitor.method or "GET",
            expected_status_codes=_parse_status_codes(monitor.expected_status_codes),
            keyword=monitor.keyword,
            timeout_seconds=monitor.timeout_seconds,
            allow_private=get_settings().monitor_allow_private_targets,
        )

    if t == "tcp":
        host, port_str = monitor.target.rsplit(":", 1)
        return TCPCheck(
            monitor_id=monitor_id,
            host=host,
            port=int(port_str),
            timeout_seconds=monitor.timeout_seconds,
        )

    if t == "dns":
        return DNSCheck(
            monitor_id=monitor_id,
            target=monitor.target,
            timeout_seconds=monitor.timeout_seconds,
        )

    # Unknown type — treat as down so it shows up rather than silently doing nothing
    raise ValueError(f"Unsupported monitor type: {t!r}")


def resolve_status(monitor: Monitor, result: CheckResult) -> str:
    """The monitor's displayed status after this result.

    Call it BEFORE updating the consecutive counters: it looks at the failures
    counted so far plus this one.
    """
    if result.status == "up":
        return "up"

    if monitor.consecutive_failures + 1 >= monitor.retry_count + 1:
        return "down"

    return monitor.current_status


def _parse_status_codes(raw: str | None) -> list[int]:
    """Accepted status codes as stored on the monitor.

    The monitors API stores a JSON list ("[200, 201]"); older rows may hold
    "200,201". Anything that isn't a valid HTTP status is ignored, and an empty
    result falls back to 200.
    """
    if not raw:
        return [200]
    try:
        values = json.loads(raw)
    except ValueError:
        values = raw.split(",")
    if not isinstance(values, list):
        values = [values]

    codes: list[int] = []
    for value in values:
        try:
            code = int(str(value).strip())
        except ValueError:
            continue
        if 100 <= code <= 599 and code not in codes:
            codes.append(code)
    return codes or [200]
