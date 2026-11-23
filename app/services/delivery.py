import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_delivery import DeliveryStatus, NotificationDelivery
from app.services.email.base import PermanentEmailError, RetryableEmailError
from app.services.retry import RetryAfterTooLong
from app.services.webhook.base import PermanentWebhookError, RetryableWebhookError

logger = logging.getLogger(__name__)

RETRYABLE_ERRORS: tuple[type[Exception], ...] = (RetryableEmailError, RetryableWebhookError)
PERMANENT_ERRORS: tuple[type[Exception], ...] = (PermanentEmailError, PermanentWebhookError)

# Returns the provider's message id (or None). Should already retry quickly,
# e.g. lambda: call_with_retry(lambda: provider.send(msg), retry_on=RETRYABLE_ERRORS)
SendFn = Callable[[], Awaitable[str | None]]
Clock = Callable[[], datetime]


class DeliveryOutcome(StrEnum):
    SENT = "sent"
    ALREADY_SENT = "already_sent"  # someone sent it before: nothing done
    ALREADY_FAILED = "already_failed"  # failed permanently before: nothing done
    SKIPPED = "skipped"  # another worker owns it, or it isn't due yet
    REQUEUED = "requeued"  # temporary failure, next_attempt_at is set
    FAILED = "failed"  # permanent failure, or out of delivery runs


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_error(exc: BaseException) -> str:
    # Message only, never a traceback: causes may contain URLs with tokens
    return f"{type(exc).__name__}: {exc}"[:500]


async def _claim(
    session: AsyncSession, key: str, channel_type: str, now: datetime, lease: timedelta
) -> int | None:
    """Try to own this delivery. Returns our run number, or None if we can't."""
    session.add(
        NotificationDelivery(
            idempotency_key=key,
            channel_type=channel_type,
            status=DeliveryStatus.SENDING.value,
            attempts=1,
            locked_until=now + lease,
        )
    )
    try:
        await session.commit()
        return 1
    except IntegrityError:
        await session.rollback()  # the key already exists

    # Take over a row that is due again, or whose worker crashed (lease expired).
    # One UPDATE with conditions, so two workers can't both take it.
    Delivery = NotificationDelivery
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Delivery)
            .where(
                Delivery.idempotency_key == key,
                or_(
                    and_(
                        Delivery.status == DeliveryStatus.PENDING.value,
                        or_(Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= now),
                    ),
                    and_(
                        Delivery.status == DeliveryStatus.SENDING.value,
                        Delivery.locked_until <= now,
                    ),
                ),
            )
            .values(
                status=DeliveryStatus.SENDING.value,
                locked_until=now + lease,
                next_attempt_at=None,
                attempts=Delivery.attempts + 1,
            )
        ),
    )
    await session.commit()
    if result.rowcount != 1:
        return None
    attempts = await session.scalar(
        select(Delivery.attempts).where(Delivery.idempotency_key == key)
    )
    return attempts


async def _finish(
    session: AsyncSession, key: str, run: int, **values: object
) -> None:
    """Record the result, but only if we still own the delivery."""
    Delivery = NotificationDelivery
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Delivery)
            .where(
                Delivery.idempotency_key == key,
                Delivery.status == DeliveryStatus.SENDING.value,
                Delivery.attempts == run,
            )
            .values(locked_until=None, **values)
        ),
    )
    await session.commit()
    if result.rowcount != 1:
        # Our lease ran out and another worker took over; its result wins
        logger.warning(
            "delivery lease expired before the result was saved",
            extra={"idempotency_key": key},
        )


async def deliver_once(
    session: AsyncSession,
    *,
    idempotency_key: str,
    channel_type: str,
    send: SendFn,
    clock: Clock = _utcnow,
    lease: timedelta = timedelta(minutes=5),
    requeue_delay: timedelta = timedelta(minutes=5),
    max_runs: int = 5,
) -> DeliveryOutcome:
    """Send a notification at most once per idempotency key.

    Uses and commits `session`, so give it a session with nothing else pending.
    `lease` must be longer than a full `send` including its quick retries.

    If a worker crashes after the provider accepted the message but before the
    result is saved, the next run sends it again. No system can rule that out,
    so receivers get the key too (webhooks: X-UpTime-Delivery) to drop repeats.
    """
    run = await _claim(session, idempotency_key, channel_type, clock(), lease)
    if run is None:
        status = await session.scalar(
            select(NotificationDelivery.status).where(
                NotificationDelivery.idempotency_key == idempotency_key
            )
        )
        if status == DeliveryStatus.SENT:
            return DeliveryOutcome.ALREADY_SENT
        if status == DeliveryStatus.FAILED:
            return DeliveryOutcome.ALREADY_FAILED
        return DeliveryOutcome.SKIPPED

    log_extra = {"idempotency_key": idempotency_key, "channel": channel_type, "run": run}

    try:
        message_id = await send()
    except RetryAfterTooLong as exc:
        await _finish(
            session,
            idempotency_key,
            run,
            status=DeliveryStatus.PENDING.value,
            next_attempt_at=clock() + timedelta(seconds=exc.retry_after),
            last_error=_safe_error(exc.__cause__ or exc),
        )
        logger.warning("delivery re-queued (server asked to wait)", extra=log_extra)
        return DeliveryOutcome.REQUEUED
    except RETRYABLE_ERRORS as exc:
        if run >= max_runs:
            await _finish(
                session,
                idempotency_key,
                run,
                status=DeliveryStatus.FAILED.value,
                last_error=_safe_error(exc),
            )
            logger.error("delivery failed: out of runs", extra=log_extra)
            return DeliveryOutcome.FAILED
        await _finish(
            session,
            idempotency_key,
            run,
            status=DeliveryStatus.PENDING.value,
            next_attempt_at=clock() + requeue_delay,
            last_error=_safe_error(exc),
        )
        logger.warning("delivery re-queued after retries", extra=log_extra)
        return DeliveryOutcome.REQUEUED
    except PERMANENT_ERRORS as exc:
        await _finish(
            session,
            idempotency_key,
            run,
            status=DeliveryStatus.FAILED.value,
            last_error=_safe_error(exc),
        )
        logger.error("delivery failed permanently", extra=log_extra)
        return DeliveryOutcome.FAILED
    except Exception as exc:
        # A bug, not a provider problem: free the delivery for a later run,
        # then let the error surface
        await _finish(
            session,
            idempotency_key,
            run,
            status=DeliveryStatus.PENDING.value,
            next_attempt_at=clock() + requeue_delay,
            last_error=_safe_error(exc),
        )
        raise

    now = clock()
    await _finish(
        session,
        idempotency_key,
        run,
        status=DeliveryStatus.SENT.value,
        sent_at=now,
        provider_message_id=message_id,
        last_error=None,
    )
    logger.info("notification delivered", extra=log_extra)
    return DeliveryOutcome.SENT
