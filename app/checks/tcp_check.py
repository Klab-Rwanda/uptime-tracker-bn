import asyncio
import time
from datetime import UTC, datetime

from app.checks.base import BaseCheck
from app.checks.result import CheckResult


class TCPCheck(BaseCheck):
    def __init__(
        self,
        monitor_id: str,
        host: str,
        port: int,
        timeout_seconds: int = 10,
    ):
        self.monitor_id = monitor_id
        self.host = host
        self.port = port
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
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout_seconds,
            )
            writer.close()
            await writer.wait_closed()
        except TimeoutError:
            return self._result(
                "down", checked_at, start,
                f"Connection timed out after {self.timeout_seconds}s",
            )
        except OSError as exc:
            # OSError covers connection refused, host unreachable, etc.
            # We only report the error type, not the message, because the
            # message can contain internal IPs or hostnames.
            return self._result(
                "down", checked_at, start,
                f"Connection failed ({type(exc).__name__})",
            )
        return self._result("up", checked_at, start)
