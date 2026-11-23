from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    to: list[str]
    subject: str
    html: str
    text: str
    # Identifies this notification. Dedupe happens in C-20, not here.
    idempotency_key: str


@dataclass(frozen=True)
class SendResult:
    provider: str
    message_id: str | None


class EmailError(Exception):
    """Base class for every email sending failure."""


class RetryableEmailError(EmailError):
    """Temporary problem (timeout, rate limit, provider down). Safe to retry."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentEmailError(EmailError):
    """Retrying will not help (bad request, bad API key, no recipients)."""


class EmailProvider(Protocol):
    async def send(self, message: EmailMessage) -> SendResult: ...

    async def aclose(self) -> None: ...
