import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.email.base import PermanentEmailError, RetryableEmailError
from app.services.notifications.delivery import DeliveryStatus
from app.services.notifications.dispatcher import (
    AttemptOutcome,
    process_delivery,
    process_due,
)
from app.services.notifications.retry import RetryPolicy
from app.services.webhook.base import PermanentWebhookError

# The `store` fixture (tests/conftest.py) runs every test against the in-memory
# store, the SQL store on SQLite, and the SQL store on PostgreSQL when available.

POLICY = RetryPolicy(max_attempts=3, base_delay=10.0, max_delay=100.0)


class MaxRng:
    """Always the top of the jitter range, so delays are predictable: 10 s, 20 s, ..."""

    def uniform(self, a, b):
        return b


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Sender:
    """Scripted send function: each call uses the next result (an exception or a message id)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def __call__(self, record):
        self.calls += 1
        await asyncio.sleep(0)  # let other workers run, like a real network call would
        result = self.results.pop(0) if self.results else "msg-default"
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def clock():
    return FakeClock()


async def make_delivery(store, clock, key="inc_1:opened:chan_1"):
    record, _ = await store.create_if_absent(
        idempotency_key=key,
        channel_id="chan_1",
        payload={"incident_id": "inc_1"},
        now=clock(),
    )
    return record


async def run(store, clock, delivery_id, send, **kwargs):
    kwargs.setdefault("policy", POLICY)
    return await process_delivery(
        store, delivery_id, send, clock=clock, rng=MaxRng(), **kwargs
    )


# --- creating deliveries -------------------------------------------------


async def test_create_is_idempotent_by_key(store, clock):
    first, created_first = await store.create_if_absent(
        idempotency_key="k", channel_id="c1", payload={}, now=clock()
    )
    second, created_second = await store.create_if_absent(
        idempotency_key="k", channel_id="c1", payload={}, now=clock()
    )
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(await store.list_due(now=clock())) == 1


async def test_concurrent_creates_with_the_same_key_make_one_delivery(store, clock):
    results = await asyncio.gather(
        *(
            store.create_if_absent(idempotency_key="k", channel_id="c1", payload={}, now=clock())
            for _ in range(10)
        )
    )
    assert sum(1 for _, created in results if created) == 1
    assert len({record.id for record, _ in results}) == 1


async def test_returned_records_are_copies(store, clock):
    record = await make_delivery(store, clock)
    record.status = DeliveryStatus.SENT
    assert (await store.get(record.id)).status is DeliveryStatus.PENDING


async def test_unknown_delivery_is_ignored(store, clock):
    assert await store.get("nope") is None
    assert await store.claim("nope", now=clock(), lease_seconds=60) is None


# --- one attempt ----------------------------------------------------------


async def test_success(store, clock):
    delivery = await make_delivery(store, clock)
    sender = Sender("brevo-123")

    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.SENT

    record = await store.get(delivery.id)
    assert record.status is DeliveryStatus.SENT
    assert record.attempts == 1
    assert record.provider_message_id == "brevo-123"
    assert record.sent_at == clock()
    assert sender.calls == 1


async def test_finished_delivery_is_never_sent_again(store, clock):
    delivery = await make_delivery(store, clock)
    sender = Sender("msg-1")
    await run(store, clock, delivery.id, sender)

    clock.advance(3600)
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.SKIPPED
    assert sender.calls == 1


async def test_retryable_failure_is_retried_after_the_delay(store, clock):
    delivery = await make_delivery(store, clock)
    start = clock()
    sender = Sender(RetryableEmailError("503 from provider"))

    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.RETRY_SCHEDULED
    record = await store.get(delivery.id)
    assert record.status is DeliveryStatus.PENDING
    assert record.attempts == 1
    assert record.next_attempt_at == start + timedelta(seconds=10)
    assert record.last_error == "503 from provider"

    clock.advance(9)  # not due yet
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.SKIPPED
    assert sender.calls == 1

    clock.advance(1)  # due now
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.SENT
    record = await store.get(delivery.id)
    assert record.attempts == 2
    assert record.status is DeliveryStatus.SENT
    assert record.last_error is None


async def test_retry_after_from_the_provider_is_honored(store, clock):
    delivery = await make_delivery(store, clock)
    start = clock()
    sender = Sender(RetryableEmailError("rate limited", retry_after=120.0))

    await run(store, clock, delivery.id, sender)
    record = await store.get(delivery.id)
    assert record.next_attempt_at == start + timedelta(seconds=120)


async def test_permanent_failure_is_not_retried(store, clock):
    delivery = await make_delivery(store, clock)
    sender = Sender(PermanentWebhookError("endpoint returned 404"))

    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.FAILED
    record = await store.get(delivery.id)
    assert record.status is DeliveryStatus.FAILED
    assert record.attempts == 1

    clock.advance(3600)
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.SKIPPED
    assert sender.calls == 1


async def test_gives_up_after_max_attempts(store, clock):
    delivery = await make_delivery(store, clock)
    sender = Sender(
        RetryableEmailError("1"), RetryableEmailError("2"), RetryableEmailError("3")
    )

    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.RETRY_SCHEDULED
    clock.advance(10)
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.RETRY_SCHEDULED
    clock.advance(20)
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.FAILED

    record = await store.get(delivery.id)
    assert record.status is DeliveryStatus.FAILED
    assert record.attempts == 3
    assert record.last_error == "3"
    assert sender.calls == 3


async def test_unexpected_error_is_retried_without_leaking_its_message(store, clock):
    delivery = await make_delivery(store, clock)
    sender = Sender(RuntimeError("postgres://user:hunter2@host/db"))

    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.RETRY_SCHEDULED
    record = await store.get(delivery.id)
    assert record.last_error == "Unexpected error (RuntimeError)"
    assert "hunter2" not in record.last_error


async def test_slow_send_times_out_and_is_retried(store, clock):
    delivery = await make_delivery(store, clock)

    async def slow(record):
        await asyncio.sleep(1)

    outcome = await run(store, clock, delivery.id, slow, lease_seconds=0.1)
    assert outcome == AttemptOutcome.RETRY_SCHEDULED
    assert (await store.get(delivery.id)).last_error == "Send timed out"


# --- several workers ------------------------------------------------------


async def test_two_workers_send_once(store, clock):
    delivery = await make_delivery(store, clock)
    sender = Sender("msg-1")

    outcomes = await asyncio.gather(*(run(store, clock, delivery.id, sender) for _ in range(5)))

    assert sorted(o.value for o in outcomes) == sorted(["sent"] + ["skipped"] * 4)
    assert sender.calls == 1
    assert (await store.get(delivery.id)).attempts == 1


async def test_delivery_of_a_crashed_worker_is_picked_up_after_the_lease(store, clock):
    delivery = await make_delivery(store, clock)
    # A worker claims it and then dies without finishing
    assert await store.claim(delivery.id, now=clock(), lease_seconds=60) is not None

    sender = Sender("msg-1")
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.SKIPPED
    assert sender.calls == 0

    clock.advance(61)
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.SENT
    assert (await store.get(delivery.id)).attempts == 2


async def test_poison_delivery_is_abandoned(store, clock):
    delivery = await make_delivery(store, clock)
    for _ in range(POLICY.max_attempts):  # workers keep dying on it
        assert await store.claim(delivery.id, now=clock(), lease_seconds=60) is not None
        clock.advance(61)

    sender = Sender()
    assert await run(store, clock, delivery.id, sender) == AttemptOutcome.FAILED
    assert sender.calls == 0
    record = await store.get(delivery.id)
    assert record.status is DeliveryStatus.FAILED
    assert record.attempts == POLICY.max_attempts + 1


async def test_stale_worker_result_is_rejected(store, clock):
    delivery = await make_delivery(store, clock)
    first = await store.claim(delivery.id, now=clock(), lease_seconds=60)
    clock.advance(61)  # the first worker is too slow
    second = await store.claim(delivery.id, now=clock(), lease_seconds=60)
    assert (first.attempts, second.attempts) == (1, 2)

    assert await store.mark_sent(
        delivery.id, attempt=2, now=clock(), provider_message_id="B"
    ) is True
    # The slow worker finally reports in, and is ignored
    assert await store.mark_sent(
        delivery.id, attempt=1, now=clock(), provider_message_id="A"
    ) is False
    assert await store.mark_failed(delivery.id, attempt=1, now=clock(), error="x") is False
    assert await store.schedule_retry(
        delivery.id, attempt=1, now=clock(), next_attempt_at=clock(), error="x"
    ) is False

    record = await store.get(delivery.id)
    assert record.status is DeliveryStatus.SENT
    assert record.provider_message_id == "B"


async def test_late_finish_is_accepted_if_nobody_else_claimed(store, clock):
    delivery = await make_delivery(store, clock)
    await store.claim(delivery.id, now=clock(), lease_seconds=60)
    clock.advance(61)
    assert await store.mark_sent(
        delivery.id, attempt=1, now=clock(), provider_message_id="A"
    ) is True


# --- finding work -----------------------------------------------------------


async def test_list_due_returns_only_claimable_deliveries_oldest_first(store, clock):
    d1 = await make_delivery(store, clock, key="k1")
    d2 = await make_delivery(store, clock, key="k2")
    d3 = await make_delivery(store, clock, key="k3")

    await store.claim(d1.id, now=clock(), lease_seconds=60)  # being worked on
    await run(store, clock, d2.id, Sender("msg"))  # finished

    assert [r.id for r in await store.list_due(now=clock())] == [d3.id]

    clock.advance(61)  # d1's lease has expired
    assert [r.id for r in await store.list_due(now=clock())] == [d3.id, d1.id]
    assert [r.id for r in await store.list_due(now=clock(), limit=1)] == [d3.id]


async def test_process_due_handles_a_batch(store, clock):
    for key in ("k1", "k2", "k3", "k4"):
        await make_delivery(store, clock, key=key)

    async def send(record):
        if record.idempotency_key == "k3":
            raise PermanentEmailError("bad address")
        return "ok"

    counts = await process_due(
        store, send, concurrency=2, policy=POLICY, clock=clock, rng=MaxRng()
    )
    assert counts == {AttemptOutcome.SENT: 3, AttemptOutcome.FAILED: 1}
    assert await store.list_due(now=clock()) == []


async def test_process_due_respects_batch_size(store, clock):
    for key in ("k1", "k2", "k3"):
        await make_delivery(store, clock, key=key)

    counts = await process_due(
        store, Sender(), batch_size=2, policy=POLICY, clock=clock, rng=MaxRng()
    )
    assert sum(counts.values()) == 2
    assert len(await store.list_due(now=clock())) == 1
