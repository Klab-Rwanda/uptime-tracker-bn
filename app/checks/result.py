from dataclasses import dataclass
from datetime import datetime


@dataclass
class CheckResult:
    monitor_id: str
    status: str
    response_time_ms: int
    checked_at: datetime
    error_message: str | None = None
