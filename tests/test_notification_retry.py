import random

import pytest

from app.services.email.base import PermanentEmailError, RetryableEmailError
from app.services.notifications.retry import FailureKind, RetryPolicy, classify_failure
from app.services.webhook.base import PermanentWebhookError, RetryableWebhookError


class MinRng:
    def uniform(self, a, b):
        return a


class MaxRng:
    def uniform(self, a, b):
        return b


POLICY = RetryPolicy(max_attempts=10, base_delay=5.0, max_delay=300.0)


def test_delay_grows_exponentially():
    assert [POLICY.delay_for(n, rng=MaxRng()) for n in (1, 2, 3, 4)] == [5.0, 10.0, 20.0, 40.0]
    assert [POLICY.delay_for(n, rng=MinRng()) for n in (1, 2, 3, 4)] == [2.5, 5.0, 10.0, 20.0]


def test_delay_is_capped_and_never_overflows():
    assert POLICY.delay_for(20, rng=MaxRng()) == 300.0
    assert POLICY.delay_for(10_000, rng=MaxRng()) == 300.0


def test_jitter_stays_inside_its_range():
    rng = random.Random(1)
    for _ in range(200):
        assert 10.0 <= POLICY.delay_for(3, rng=rng) <= 20.0


def test_retry_after_longer_than_backoff_is_honored():
    assert POLICY.delay_for(1, retry_after=120.0, rng=MinRng()) == 120.0


def test_retry_after_shorter_than_backoff_is_ignored():
    assert POLICY.delay_for(4, retry_after=5.0, rng=MaxRng()) == 40.0


def test_retry_after_is_capped():
    assert POLICY.delay_for(1, retry_after=10**9, rng=MinRng()) == 3600.0


@pytest.mark.parametrize("retry_after", [0, -5, None])
def test_useless_retry_after_is_ignored(retry_after):
    assert POLICY.delay_for(1, retry_after=retry_after, rng=MaxRng()) == 5.0


def test_should_retry_respects_max_attempts():
    policy = RetryPolicy(max_attempts=3)
    assert policy.should_retry(1) is True
    assert policy.should_retry(2) is True
    assert policy.should_retry(3) is False


def test_policy_validation():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=10, max_delay=5)


def test_classify_retryable_errors():
    failure = classify_failure(RetryableEmailError("rate limited", retry_after=30.0))
    assert failure.kind is FailureKind.RETRYABLE
    assert failure.retry_after == 30.0
    assert failure.message == "rate limited"

    failure = classify_failure(RetryableWebhookError("endpoint down"))
    assert failure.kind is FailureKind.RETRYABLE
    assert failure.retry_after is None


def test_classify_permanent_errors():
    assert classify_failure(PermanentEmailError("bad key")).kind is FailureKind.PERMANENT
    assert classify_failure(PermanentWebhookError("404")).kind is FailureKind.PERMANENT


def test_classify_timeout_as_retryable():
    failure = classify_failure(TimeoutError())
    assert failure.kind is FailureKind.RETRYABLE
    assert failure.message == "Send timed out"


def test_unexpected_errors_never_expose_their_message():
    failure = classify_failure(RuntimeError("postgres://user:hunter2@host/db"))
    assert failure.kind is FailureKind.UNEXPECTED
    assert "hunter2" not in failure.message
    assert "RuntimeError" in failure.message


def test_messages_are_clipped_and_single_line():
    assert len(classify_failure(RetryableEmailError("x" * 5000)).message) <= 500
    assert classify_failure(PermanentEmailError("a\nb")).message == "a b"
