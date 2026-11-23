from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class WebhookEvent:
    event_type: str  # e.g. "incident.opened"
    delivery_id: str  # stable ID for this notification (the idempotency key)
    created_at: datetime
    data: dict[str, Any]


@dataclass(frozen=True)
class WebhookResult:
    status_code: int
    duration_ms: float


class WebhookError(Exception):
    """Base class for every webhook sending failure.

    The message is safe to log. The chained cause (__cause__) may be an httpx
    error whose text contains the full URL, including any secret token in the
    path, so log these without a traceback (no logger.exception / exc_info).
    """


class RetryableWebhookError(WebhookError):
    """Temporary problem (timeout, 5xx, rate limit, DNS). Safe to retry."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentWebhookError(WebhookError):
    """Retrying will not help (unsafe URL, 4xx, redirect)."""


class WebhookSender(Protocol):
    async def send(self, url: str, secret: str, event: WebhookEvent) -> WebhookResult: ...

    async def aclose(self) -> None: ...
