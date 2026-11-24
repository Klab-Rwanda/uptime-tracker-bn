from app.checks.http_check import HTTPCheck
from app.checks.result import CheckResult
from app.models.monitor import Monitor


async def run_with_retry(monitor: Monitor) -> CheckResult:
    check = HTTPCheck(
        monitor_id=str(monitor.id),
        target=monitor.target,
        method=monitor.method or "GET",
        expected_status_codes=_parse_status_codes(monitor.expected_status_codes),
        keyword=monitor.keyword,
        timeout_seconds=monitor.timeout_seconds,
    )

    result = await check.run()

    if result.status == "down" and monitor.retry_count > 0:
        for attempt in range(monitor.retry_count):
            result = await check.run()
            if result.status == "up":
                break

    return result


def resolve_status(monitor: Monitor, result: CheckResult) -> str:
    if result.status == "up":
        return "up"

    if monitor.consecutive_failures + 1 >= monitor.retry_count + 1:
        return "down"

    return monitor.current_status


def _parse_status_codes(raw: str | None) -> list[int]:
    if not raw:
        return [200]
    return [int(c.strip()) for c in raw.split(",") if c.strip().isdigit()]
