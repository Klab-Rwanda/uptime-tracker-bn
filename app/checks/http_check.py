import time
from datetime import datetime, timezone

import httpx

from app.checks.base import BaseCheck
from app.checks.result import CheckResult


class HTTPCheck(BaseCheck):
    def __init__(
        self,
        monitor_id: str,
        target: str,
        method: str = "GET",
        expected_status_codes: list[int] | None = None,
        keyword: str | None = None,
        timeout_seconds: int = 30,
    ):
        self.monitor_id = monitor_id
        self.target = target
        self.method = method.upper()
        self.expected_status_codes = expected_status_codes or [200]
        self.keyword = keyword
        self.timeout_seconds = timeout_seconds

    async def run(self) -> CheckResult:
        checked_at = datetime.now(timezone.utc)
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.request(
                    self.method,
                    self.target,
                    timeout=self.timeout_seconds,
                )

            response_time_ms = int((time.monotonic() - start) * 1000)

            if response.status_code not in self.expected_status_codes:
                return CheckResult(
                    monitor_id=self.monitor_id,
                    status="down",
                    response_time_ms=response_time_ms,
                    checked_at=checked_at,
                    error_message=f"Unexpected status code: {response.status_code}",
                )

            if self.keyword and self.keyword not in response.text:
                return CheckResult(
                    monitor_id=self.monitor_id,
                    status="down",
                    response_time_ms=response_time_ms,
                    checked_at=checked_at,
                    error_message=f"Keyword not found: {self.keyword!r}",
                )

            return CheckResult(
                monitor_id=self.monitor_id,
                status="up",
                response_time_ms=response_time_ms,
                checked_at=checked_at,
            )

        except httpx.TimeoutException:
            response_time_ms = int((time.monotonic() - start) * 1000)
            return CheckResult(
                monitor_id=self.monitor_id,
                status="down",
                response_time_ms=response_time_ms,
                checked_at=checked_at,
                error_message=f"Request timed out after {self.timeout_seconds}s",
            )

        except Exception as e:
            response_time_ms = int((time.monotonic() - start) * 1000)
            return CheckResult(
                monitor_id=self.monitor_id,
                status="down",
                response_time_ms=response_time_ms,
                checked_at=checked_at,
                error_message=str(e),
            )
