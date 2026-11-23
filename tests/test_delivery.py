from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.notification_delivery import NotificationDelivery
from app.services.delivery import DeliveryOutcome, deliver_once
from app.services.email.base import PermanentEmailError, RetryableEmailError
from app.services.retry import RetryAfterTooLong
from app.services.webhook.base import RetryableWebhookError
from app.util.database import Base

KEY = "inc_1:opened:chan_1"
START = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def naive(value: datetime) -> datetime:
    # SQLite gives timestamps back without a timezone; all of ours are UTC
    return value.replace(tzinfo=None)


class Sender:
    """A send function that counts calls and can fail or run a side effect."""

    def __init__(self, error: Exception | None = None, during=None) -> None:
        self.calls = 0
        self.error = error
        self.during = during

    async def __call__(self) -> str | None:
        self.calls += 1
        if self.during is not None:
            await self.during()
        if self.error is not None:
            raise self.error
        return f"msg-{self.calls}"


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all, tables=[NotificationDelivery.__table__]
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


async def deliver(sessions, clock, send, key=KEY, **kwargs):
    async with sessions() as session:
        return await deliver_once(
            session, idempotency_key=key, channel_type="email", send=send, clock=clock, **kwargs
        )


async def load(sessions, key=KEY) -> NotificationDelivery:
    async with sessions() as session:
        row = await session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.idempotency_key == key)
        )
        assert row is not None
        return row


# --- the happy path and duplicates -----------------------------------------


async def test_first_delivery_is_sent_and_recorded(sessions, clock):
    send = Sender()
    assert await deliver(sessions, clock, send) == DeliveryOutcome.SENT

    row = await load(sessions)
    assert send.calls == 1
    assert row.status == "sent"
    assert row.attempts == 1
    assert row.provider_message_id == "msg-1"
    assert naive(row.sent_at) == naive(START)
    assert row.locked_until is None


async def test_same_key_is_never_sent_twice(sessions, clock):
    send = Sender()
    await deliver(sessions, clock, send)
    assert await deliver(sessions, clock, send) == DeliveryOutcome.ALREADY_SENT
    assert send.calls == 1


async def test_different_keys_are_independent(sessions, clock):
    send = Sender()
    await deliver(sessions, clock, send, key="a")
    assert await deliver(sessions, clock, send, key="b") == DeliveryOutcome.SENT
    assert send.calls == 2


async def test_second_worker_skips_while_first_is_sending(sessions, clock):
    second = Sender()
    results = {}

    async def other_worker_runs_meanwhile():
        results["second"] = await deliver(sessions, clock, second)

    first = Sender(during=other_worker_runs_meanwhile)
    assert await deliver(sessions, clock, first) == DeliveryOutcome.SENT
    assert results["second"] == DeliveryOutcome.SKIPPED
    assert second.calls == 0


# --- crashed workers and leases --------------------------------------------


async def insert_row(sessions, **values):
    async with sessions() as session:
        session.add(NotificationDelivery(idempotency_key=KEY, channel_type="email", **values))
        await session.commit()


async def test_crashed_worker_is_taken_over_after_lease_expires(sessions, clock):
    await insert_row(sessions, status="sending", attempts=1, locked_until=START)
    clock.advance(seconds=1)

    send = Sender()
    assert await deliver(sessions, clock, send) == DeliveryOutcome.SENT
    row = await load(sessions)
    assert row.attempts == 2
    assert row.status == "sent"


async def test_active_lease_is_respected(sessions, clock):
    await insert_row(
        sessions, status="sending", attempts=1, locked_until=START + timedelta(minutes=1)
    )
    send = Sender()
    assert await deliver(sessions, clock, send) == DeliveryOutcome.SKIPPED
    assert send.calls == 0


async def test_slow_worker_does_not_overwrite_the_worker_that_took_over(sessions, clock):
    takeover = Sender()

    async def lease_runs_out_and_another_worker_takes_over():
        clock.advance(minutes=6)
        assert await deliver(sessions, clock, takeover) == DeliveryOutcome.SENT

    slow = Sender(error=RetryableEmailError("timeout"), during=lease_runs_out_and_another_worker_takes_over)
    await deliver(sessions, clock, slow)

    row = await load(sessions)
    assert row.status == "sent"  # the slow worker's failure didn't overwrite it
    assert row.attempts == 2
    assert row.provider_message_id == "msg-1"


# --- failures ---------------------------------------------------------------


async def test_retryable_failure_is_requeued_then_sent_when_due(sessions, clock):
    failing = Sender(error=RetryableWebhookError("Endpoint returned 503"))
    assert await deliver(sessions, clock, failing) == DeliveryOutcome.REQUEUED

    row = await load(sessions)
    assert row.status == "pending"
    assert naive(row.next_attempt_at) == naive(START + timedelta(minutes=5))
    assert row.last_error == "RetryableWebhookError: Endpoint returned 503"

    send = Sender()
    clock.advance(minutes=4)
    assert await deliver(sessions, clock, send) == DeliveryOutcome.SKIPPED  # not due yet
    clock.advance(minutes=1)
    assert await deliver(sessions, clock, send) == DeliveryOutcome.SENT
    row = await load(sessions)
    assert row.attempts == 2
    assert row.last_error is None


async def test_out_of_runs_becomes_permanent_failure(sessions, clock):
    failing = Sender(error=RetryableEmailError("timeout"))
    outcomes = []
    for _ in range(3):
        outcomes.append(await deliver(sessions, clock, failing, max_runs=3))
        clock.advance(minutes=5)

    assert outcomes == [DeliveryOutcome.REQUEUED, DeliveryOutcome.REQUEUED, DeliveryOutcome.FAILED]
    assert await deliver(sessions, clock, failing) == DeliveryOutcome.ALREADY_FAILED
    assert failing.calls == 3


async def test_permanent_error_is_never_retried(sessions, clock):
    failing = Sender(error=PermanentEmailError("Brevo rejected the email (401)"))
    assert await deliver(sessions, clock, failing) == DeliveryOutcome.FAILED
    clock.advance(hours=1)
    assert await deliver(sessions, clock, failing) == DeliveryOutcome.ALREADY_FAILED
    assert failing.calls == 1
    assert (await load(sessions)).status == "failed"


async def test_retry_after_too_long_is_requeued_for_that_time(sessions, clock):
    cause = RetryableEmailError("Brevo returned 429", retry_after=600)
    error = RetryAfterTooLong(600)
    error.__cause__ = cause
    assert await deliver(sessions, clock, Sender(error=error)) == DeliveryOutcome.REQUEUED

    row = await load(sessions)
    assert naive(row.next_attempt_at) == naive(START + timedelta(seconds=600))
    assert row.last_error == "RetryableEmailError: Brevo returned 429"


async def test_unexpected_error_frees_the_delivery_and_is_raised(sessions, clock):
    with pytest.raises(KeyError):
        await deliver(sessions, clock, Sender(error=KeyError("bug")))
    row = await load(sessions)
    assert row.status == "pending"
    assert row.locked_until is None

    clock.advance(minutes=5)
    assert await deliver(sessions, clock, Sender()) == DeliveryOutcome.SENT


async def test_saved_error_has_no_traceback_or_url_token(sessions, clock):
    error = RetryableWebhookError("Could not connect to the webhook endpoint")
    error.__cause__ = ConnectionError("https://hooks.acme.com/secret-token-123")
    await deliver(sessions, clock, Sender(error=error))
    row = await load(sessions)
    assert "secret-token-123" not in (row.last_error or "")
    assert "Traceback" not in (row.last_error or "")


# --- database rules ---------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        {"status": "exploded", "attempts": 0},
        {"status": "sending", "attempts": 1},  # sending without a lease
        {"status": "sent", "attempts": 1},  # sent without sent_at
        {"status": "pending", "attempts": -1},
    ],
)
async def test_invalid_rows_are_rejected(sessions, values):
    with pytest.raises(IntegrityError):
        await insert_row(sessions, **values)
