import random
from dataclasses import dataclass
from enum import StrEnum

from app.services.email.base import PermanentEmailError, RetryableEmailError
from app.services.webhook.base import PermanentWebhookError, RetryableWebhookError

MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class RetryPolicy:
    # Defaults: retries spread over roughly 2.5 to 5 minutes before giving up
    max_attempts: int = 6
    base_delay: float = 10.0
    max_delay: float = 300.0
    # Upper bound on honoring a server's Retry-After, so a hostile or broken
    # endpoint can't park a delivery for days
    max_retry_after: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay <= 0:
            raise ValueError("base_delay must be positive")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must not be smaller than base_delay")

    def should_retry(self, attempts_made: int) -> bool:
        return attempts_made < self.max_attempts

    def delay_for(
        self,
        attempts_made: int,
        *,
        retry_after: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        """Seconds to wait before the next attempt (attempts_made is 1 or more).

        Exponential backoff with "equal jitter": half the delay is fixed and half is
        random, so many failing deliveries don't all retry at the same instant.
        """
        uniform = rng.uniform if rng is not None else random.uniform
        exponent = min(max(attempts_made - 1, 0), 30)  # avoids huge numbers
        ceiling = min(self.max_delay, self.base_delay * 2**exponent)
        delay = ceiling / 2 + uniform(0, ceiling / 2)
        if retry_after is not None and retry_after > 0:
            delay = max(delay, min(retry_after, self.max_retry_after))
        return delay


DEFAULT_POLICY = RetryPolicy()


class FailureKind(StrEnum):
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    message: str  # safe to store and show to users
    retry_after: float | None = None


def _clip(text: str) -> str:
    return " ".join(text.split())[:MAX_ERROR_LENGTH]


class PermanentDeliveryError(Exception):
    """A delivery that can never succeed: the channel was deleted, belongs to
    another organization, or its stored configuration can't be read.

    The message is stored and shown to users, so it must not contain secrets.
    """


_RETRYABLE = (RetryableEmailError, RetryableWebhookError)
_PERMANENT = (PermanentEmailError, PermanentWebhookError, PermanentDeliveryError)


def classify_failure(exc: BaseException) -> Failure:
    """Decide what to do about an error raised while sending.

    Errors from our own senders are written to be safe to store. For anything
    else we keep only the class name: its message could contain a URL with a
    token, a connection string, or other secrets.
    """
    if isinstance(exc, _RETRYABLE):
        return Failure(FailureKind.RETRYABLE, _clip(str(exc)), getattr(exc, "retry_after", None))
    if isinstance(exc, _PERMANENT):
        return Failure(FailureKind.PERMANENT, _clip(str(exc)))
    if isinstance(exc, TimeoutError):
        return Failure(FailureKind.RETRYABLE, "Send timed out")
    return Failure(FailureKind.UNEXPECTED, f"Unexpected error ({type(exc).__name__})")
