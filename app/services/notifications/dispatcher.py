import asyncio
import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.services.notifications.delivery import DeliveryRecord, DeliveryStore
from app.services.notifications.retry import (
    DEFAULT_POLICY,
    FailureKind,
    RetryPolicy,
    classify_failure,
)

logger = logging.getLogger(__name__)

# A send function does the real work (email or webhook) and returns the provider's
# message id, if it has one. It raises on failure.
SendFn = Callable[[DeliveryRecord], Awaitable[str | None]]
Clock = Callable[[], datetime]

# A worker must give up before its lease runs out, so it never keeps sending
# after another worker may have taken over
SEND_TIMEOUT_FRACTION = 0.8


def utcnow() -> datetime:
    return datetime.now(UTC)


class AttemptOutcome(StrEnum):
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    SKIPPED = "skipped"  # not due, claimed by someone else, finished, or a stale result


async def process_delivery(
    store: DeliveryStore,
    delivery_id: str,
    send: SendFn,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    lease_seconds: float = 60.0,
    clock: Clock = utcnow,
    rng: random.Random | None = None,
) -> AttemptOutcome:
    """Make one delivery attempt, and record what happened."""
    record = await store.claim(delivery_id, now=clock(), lease_seconds=lease_seconds)
    if record is None:
        return AttemptOutcome.SKIPPED

    attempt = record.attempts
    context = {"delivery_id": record.id, "channel_id": record.channel_id, "attempt": attempt}

    # A delivery claimed again and again without ever finishing is crashing its
    # workers. Stop it instead of looping forever.
    if attempt > policy.max_attempts:
        applied = await store.mark_failed(
            record.id,
            attempt=attempt,
            now=clock(),
            error="Gave up: the delivery was claimed too many times without finishing",
        )
        logger.error("delivery abandoned", extra=context)
        return AttemptOutcome.FAILED if applied else AttemptOutcome.SKIPPED

    try:
        provider_message_id = await asyncio.wait_for(
            send(record), timeout=lease_seconds * SEND_TIMEOUT_FRACTION
        )
    except Exception as exc:  # CancelledError is not caught on purpose (see notes)
        failure = classify_failure(exc)
        if failure.kind is FailureKind.UNEXPECTED:
            logger.error("unexpected error while sending", extra=context, exc_info=exc)

        now = clock()
        if failure.kind is FailureKind.PERMANENT or not policy.should_retry(attempt):
            applied = await store.mark_failed(
                record.id, attempt=attempt, now=now, error=failure.message
            )
            outcome = AttemptOutcome.FAILED
            logger.warning("delivery failed", extra={**context, "error": failure.message})
        else:
            delay = policy.delay_for(attempt, retry_after=failure.retry_after, rng=rng)
            applied = await store.schedule_retry(
                record.id,
                attempt=attempt,
                now=now,
                next_attempt_at=now + timedelta(seconds=delay),
                error=failure.message,
            )
            outcome = AttemptOutcome.RETRY_SCHEDULED
            logger.info(
                "delivery will be retried",
                extra={**context, "retry_in_seconds": round(delay, 1), "error": failure.message},
            )

        if not applied:
            logger.warning("stale worker result discarded", extra=context)
            return AttemptOutcome.SKIPPED
        return outcome

    applied = await store.mark_sent(
        record.id, attempt=attempt, now=clock(), provider_message_id=provider_message_id
    )
    if not applied:
        # We sent it, but another worker owns the record now, so a duplicate is possible
        logger.warning("sent, but the result was stale (possible duplicate)", extra=context)
        return AttemptOutcome.SKIPPED
    logger.info("delivery sent", extra=context)
    return AttemptOutcome.SENT


async def process_due(
    store: DeliveryStore,
    send: SendFn,
    *,
    batch_size: int = 50,
    concurrency: int = 5,
    policy: RetryPolicy = DEFAULT_POLICY,
    lease_seconds: float = 60.0,
    clock: Clock = utcnow,
    rng: random.Random | None = None,
) -> dict[AttemptOutcome, int]:
    """Handle everything that is due right now. A worker loop will call this repeatedly."""
    due = await store.list_due(now=clock(), limit=batch_size)
    semaphore = asyncio.Semaphore(concurrency)

    async def run(record: DeliveryRecord) -> AttemptOutcome:
        async with semaphore:
            return await process_delivery(
                store,
                record.id,
                send,
                policy=policy,
                lease_seconds=lease_seconds,
                clock=clock,
                rng=rng,
            )

    results = await asyncio.gather(*(run(record) for record in due), return_exceptions=True)

    counts: Counter[AttemptOutcome] = Counter()
    for result in results:
        if isinstance(result, BaseException):
            # One crash (for example a database blip) must not stop the rest of the batch
            logger.error("delivery processing crashed", exc_info=result)
            continue
        counts[result] += 1
    return dict(counts)
