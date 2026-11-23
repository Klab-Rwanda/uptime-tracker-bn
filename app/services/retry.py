import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

Sleep = Callable[[float], Awaitable[None]]
Random = Callable[[], float]  # returns a float in [0, 1)


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how long to retry a failed send.

    Delay before retry n (1-based) is base_delay * multiplier ** (n - 1), capped
    at max_delay, then reduced by up to `jitter` (0.5 = up to half) so many
    failing notifications don't all retry at the same moment.
    """

    max_attempts: int = 5  # total tries, including the first one
    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.5

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay <= 0:
            raise ValueError("base_delay must be positive")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be at least base_delay")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")


class RetryAfterTooLong(Exception):
    """The server asked us to wait longer than max_delay.

    Raised from the original error (available as __cause__) so the caller can
    re-queue the notification for later instead of blocking a worker.
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Server asked to retry after {retry_after:g}s")
        self.retry_after = retry_after


def compute_delay(
    policy: RetryPolicy,
    retry_number: int,
    retry_after: float | None = None,
    rand: float = 1.0,
) -> float:
    """Seconds to wait before retry `retry_number` (1 = first retry).

    `rand` is in [0, 1]: 1.0 gives the full backoff, 0.0 the most jitter.
    A server's retry_after is a minimum: we never retry sooner than asked.
    """
    backoff = min(policy.max_delay, policy.base_delay * policy.multiplier ** (retry_number - 1))
    delay = backoff * (1 - policy.jitter * (1 - rand))
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


async def call_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[Exception], ...],
    policy: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
    rand: Random = random.random,
    description: str = "operation",
) -> T:
    """Run `operation`, retrying errors in `retry_on` with backoff.

    Anything not in `retry_on` (permanent errors, bugs) is raised at once.
    When attempts run out, the last retryable error is raised unchanged, so
    callers can still tell retryable from permanent failures.
    """
    policy = policy or RetryPolicy()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            if attempt == policy.max_attempts:
                logger.warning(
                    "giving up after retries",
                    extra={
                        "operation": description,
                        "attempts": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                raise

            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None and retry_after > policy.max_delay:
                raise RetryAfterTooLong(retry_after) from exc

            delay = compute_delay(policy, attempt, retry_after, rand())
            # No exc_info: some causes (httpx errors) contain URLs with secret tokens
            logger.warning(
                "retrying after error",
                extra={
                    "operation": description,
                    "attempt": attempt,
                    "delay_seconds": round(delay, 2),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            await sleep(delay)

    raise AssertionError("unreachable")  # the loop always returns or raises
