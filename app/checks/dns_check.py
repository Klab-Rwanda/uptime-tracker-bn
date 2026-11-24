import time
from datetime import datetime, timezone

import dns.resolver

from app.checks.base import BaseCheck
from app.checks.result import CheckResult


class DNSCheck(BaseCheck):
    def __init__(
        self,
        monitor_id: str,
        target: str,
        timeout_seconds: int = 10,
    ):
        self.monitor_id = monitor_id
        self.target = target
        self.timeout_seconds = timeout_seconds

    async def run(self) -> CheckResult:
        checked_at = datetime.now(timezone.utc)
        start = time.monotonic()

        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = self.timeout_seconds
            resolver.resolve(self.target, "A")
            response_time_ms = int((time.monotonic() - start) * 1000)
            return CheckResult(
                monitor_id=self.monitor_id,
                status="up",
                response_time_ms=response_time_ms,
                checked_at=checked_at,
            )

        except dns.resolver.NXDOMAIN:
            response_time_ms = int((time.monotonic() - start) * 1000)
            return CheckResult(
                monitor_id=self.monitor_id,
                status="down",
                response_time_ms=response_time_ms,
                checked_at=checked_at,
                error_message=f"Domain {self.target!r} does not exist",
            )

        except dns.resolver.Timeout:
            response_time_ms = int((time.monotonic() - start) * 1000)
            return CheckResult(
                monitor_id=self.monitor_id,
                status="down",
                response_time_ms=response_time_ms,
                checked_at=checked_at,
                error_message=f"DNS lookup timed out after {self.timeout_seconds}s",
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
