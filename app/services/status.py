import asyncio
import json
from collections.abc import Awaitable, Callable

from app.checks.http_check import HTTPCheck
from app.checks.result import CheckResult
from app.models.monitor import Monitor
from app.util.config import get_settings

Sleep = Callable[[float], Awaitable[None]]


async def run_with_retry(monitor: Monitor, *, sleep: Sleep = asyncio.sleep) -> CheckResult:
    """Run the monitor's check, retrying a failure up to retry_count times.

    Waits retry_interval_seconds between attempts, so a struggling site isn't
    hit several times in a row. Only the final result is returned (and stored).
    """
    check = HTTPCheck(
        monitor_id=str(monitor.id),
        target=monitor.target,
        method=monitor.method or "GET",
        expected_status_codes=_parse_status_codes(monitor.expected_status_codes),
        keyword=monitor.keyword,
        timeout_seconds=monitor.timeout_seconds,
        allow_private=get_settings().monitor_allow_private_targets,
    )

    result = await check.run()
    for _ in range(max(monitor.retry_count, 0)):
        if result.status == "up":
            break
        await sleep(max(monitor.retry_interval_seconds, 0))
        result = await check.run()
    return result


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
