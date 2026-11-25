import time
from datetime import UTC, datetime

import dns.asyncresolver
import dns.exception
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

    def _result(
        self, status: str, checked_at: datetime, start: float, error: str | None = None
    ) -> CheckResult:
        return CheckResult(
            monitor_id=self.monitor_id,
            status=status,
            response_time_ms=int((time.monotonic() - start) * 1000),
            checked_at=checked_at,
            error_message=error,
        )

    async def run(self) -> CheckResult:
        checked_at = datetime.now(UTC)
        start = time.monotonic()
        # The async resolver: the blocking one froze the whole event loop
        # (every request and every other check) for the length of each lookup
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = self.timeout_seconds
        try:
            await resolver.resolve(self.target, "A")
        except dns.resolver.NXDOMAIN:
            return self._result(
                "down", checked_at, start, f"Domain {self.target!r} does not exist"
            )
        except dns.exception.Timeout:
            return self._result(
                "down", checked_at, start, f"DNS lookup timed out after {self.timeout_seconds}s"
            )
        except dns.exception.DNSException as exc:
            # e.g. NoAnswer, NoNameservers: report the kind of failure, not internals
            return self._result("down", checked_at, start, f"DNS lookup failed ({type(exc).__name__})")
        return self._result("up", checked_at, start)
