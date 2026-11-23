import logging
from datetime import UTC, datetime

import pytest

from app.services.email.base import (
    EmailMessage,
    PermanentEmailError,
    RetryableEmailError,
)
from app.services.email.fake import InMemoryEmailProvider
from app.services.retry import (
    RetryAfterTooLong,
    RetryPolicy,
    call_with_retry,
    compute_delay,
)
from app.services.webhook.base import (
    PermanentWebhookError,
    RetryableWebhookError,
    WebhookEvent,
)
from app.services.webhook.fake import InMemoryWebhookSender

RETRYABLE = (RetryableEmailError, RetryableWebhookError)


class FakeSleep:
    """Records requested delays instead of waiting, so tests run instantly."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def failing_then_ok(errors: list[Exception], result="ok"):
    """An operation that raises each error in turn, then returns `result`."""
    calls = {"count": 0}

    async def operation():
        calls["count"] += 1
        if errors:
            raise errors.pop(0)
        return result

    return operation, calls


def no_jitter() -> float:
    return 1.0


# --- compute_delay ---------------------------------------------------------


def test_delays_grow_exponentially_and_are_capped():
    policy = RetryPolicy(base_delay=1, multiplier=2, max_delay=10, jitter=0.5)
    delays = [compute_delay(policy, n, rand=1.0) for n in range(1, 7)]
    assert delays == [1, 2, 4, 8, 10, 10]


def test_jitter_shortens_the_delay_by_at_most_the_jitter_fraction():
    policy = RetryPolicy(base_delay=4, jitter=0.5)
    assert compute_delay(policy, 1, rand=1.0) == 4
    assert compute_delay(policy, 1, rand=0.5) == 3
    assert compute_delay(policy, 1, rand=0.0) == 2  # never below half


def test_zero_jitter_is_deterministic():
    policy = RetryPolicy(base_delay=4, jitter=0)
    assert compute_delay(policy, 1, rand=0.0) == 4


def test_retry_after_is_a_minimum():
    policy = RetryPolicy(base_delay=1, jitter=0)
    assert compute_delay(policy, 1, retry_after=30) == 30
    # A shorter retry_after doesn't shorten our own backoff
    assert compute_delay(policy, 4, retry_after=2) == 8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay": 0},
        {"multiplier": 0.5},
        {"base_delay": 10, "max_delay": 5},
        {"jitter": -0.1},
        {"jitter": 1.5},
    ],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# --- call_with_retry -------------------------------------------------------


async def test_success_on_first_try_does_not_sleep():
    sleep = FakeSleep()
    operation, calls = failing_then_ok([])
    assert await call_with_retry(operation, retry_on=RETRYABLE, sleep=sleep) == "ok"
    assert calls["count"] == 1
    assert sleep.delays == []


async def test_retryable_errors_are_retried_with_backoff():
    sleep = FakeSleep()
    operation, calls = failing_then_ok(
        [RetryableEmailError("timeout"), RetryableEmailError("503"), RetryableEmailError("503")]
    )
    policy = RetryPolicy(base_delay=1, multiplier=2, jitter=0)
    result = await call_with_retry(
        operation, retry_on=RETRYABLE, policy=policy, sleep=sleep, rand=no_jitter
    )
    assert result == "ok"
    assert calls["count"] == 4
    assert sleep.delays == [1, 2, 4]


async def test_server_retry_after_is_honored():
    sleep = FakeSleep()
    operation, _ = failing_then_ok([RetryableWebhookError("429", retry_after=30)])
    await call_with_retry(
        operation,
        retry_on=RETRYABLE,
        policy=RetryPolicy(base_delay=1, max_delay=60),
        sleep=sleep,
        rand=no_jitter,
    )
    assert sleep.delays == [30]


async def test_retry_after_longer_than_max_delay_gives_up_at_once():
    sleep = FakeSleep()
    original = RetryableEmailError("429", retry_after=600)
    operation, calls = failing_then_ok([original])

    with pytest.raises(RetryAfterTooLong) as info:
        await call_with_retry(
            operation, retry_on=RETRYABLE, policy=RetryPolicy(max_delay=60), sleep=sleep
        )
    assert info.value.retry_after == 600
    assert info.value.__cause__ is original
    assert calls["count"] == 1
    assert sleep.delays == []


@pytest.mark.parametrize(
    "error",
    [PermanentEmailError("bad key"), PermanentWebhookError("410 gone"), KeyError("bug")],
)
async def test_permanent_errors_and_bugs_are_not_retried(error):
    sleep = FakeSleep()
    operation, calls = failing_then_ok([error])
    with pytest.raises(type(error)):
        await call_with_retry(operation, retry_on=RETRYABLE, sleep=sleep)
    assert calls["count"] == 1
    assert sleep.delays == []


async def test_gives_up_after_max_attempts_with_the_last_error():
    sleep = FakeSleep()
    errors = [RetryableEmailError(f"fail {i}") for i in range(10)]
    operation, calls = failing_then_ok(errors)

    with pytest.raises(RetryableEmailError, match="fail 2"):
        await call_with_retry(
            operation, retry_on=RETRYABLE, policy=RetryPolicy(max_attempts=3), sleep=sleep
        )
    assert calls["count"] == 3
    assert len(sleep.delays) == 2  # no sleep after the final attempt


async def test_max_attempts_one_means_no_retries():
    sleep = FakeSleep()
    operation, calls = failing_then_ok([RetryableEmailError("timeout")])
    with pytest.raises(RetryableEmailError):
        await call_with_retry(
            operation, retry_on=RETRYABLE, policy=RetryPolicy(max_attempts=1), sleep=sleep
        )
    assert calls["count"] == 1
    assert sleep.delays == []


async def test_jitter_uses_the_injected_random_source():
    sleep = FakeSleep()
    operation, _ = failing_then_ok([RetryableEmailError("x")])
    await call_with_retry(
        operation,
        retry_on=RETRYABLE,
        policy=RetryPolicy(base_delay=4, jitter=0.5),
        sleep=sleep,
        rand=lambda: 0.0,
    )
    assert sleep.delays == [2]


async def test_retry_logs_have_no_traceback(caplog):
    caplog.set_level(logging.WARNING, logger="app.services.retry")
    operation, _ = failing_then_ok([RetryableWebhookError("Endpoint returned 503")])
    await call_with_retry(operation, retry_on=RETRYABLE, sleep=FakeSleep())

    records = [r for r in caplog.records if r.name == "app.services.retry"]
    assert records
    assert all(r.exc_info is None for r in records)
    assert records[0].error_type == "RetryableWebhookError"


# --- with the real fake senders --------------------------------------------


async def test_wraps_email_provider():
    provider = InMemoryEmailProvider(fail_with=RetryableEmailError("down"))
    message = EmailMessage(to=["a@b.com"], subject="s", html="h", text="t", idempotency_key="k")
    sleep = FakeSleep()

    async def recover(seconds: float) -> None:
        await sleep(seconds)
        provider.fail_with = None  # the provider comes back after the first wait

    result = await call_with_retry(
        lambda: provider.send(message), retry_on=RETRYABLE, sleep=recover
    )
    assert result.message_id == "memory-1"
    assert provider.sent == [message]
    assert len(sleep.delays) == 1


async def test_wraps_webhook_sender():
    sender = InMemoryWebhookSender(fail_with=RetryableWebhookError("503"))
    event = WebhookEvent("incident.opened", "d1", datetime(2026, 11, 20, tzinfo=UTC), {})

    async def recover(seconds: float) -> None:
        sender.fail_with = None

    result = await call_with_retry(
        lambda: sender.send("https://hooks.acme.com/x", "secret", event),
        retry_on=RETRYABLE,
        sleep=recover,
    )
    assert result.status_code == 200
    assert len(sender.sent) == 1
