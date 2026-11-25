from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.checks.result import CheckResult
from app.services import status
from app.services.status import _parse_status_codes, resolve_status, run_with_retry


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[200, 201]", [200, 201]),  # what the monitors API stores
        ("[204]", [204]),
        ("200,201", [200, 201]),  # older comma-separated rows
        (" 200 , 302 ", [200, 302]),
        ("200", [200]),
        ("[200, 200, 201]", [200, 201]),
        ("[200, 99, 600, \"x\"]", [200]),  # not HTTP status codes
        ("[]", [200]),
        ("", [200]),
        (None, [200]),
        ("garbage", [200]),
    ],
)
def test_parse_status_codes(raw, expected):
    assert _parse_status_codes(raw) == expected


def result(state: str) -> CheckResult:
    return CheckResult(monitor_id="m", status=state, response_time_ms=1, checked_at=datetime.now(UTC))


def monitor(**overrides):
    values = {
        "id": "m",
        "target": "https://status.acme.com/",
        "method": "GET",
        "expected_status_codes": "[200]",
        "keyword": None,
        "timeout_seconds": 10,
        "retry_count": 2,
        "retry_interval_seconds": 30,
        "consecutive_failures": 0,
        "current_status": "pending",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCheck:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    async def run(self):
        self.calls += 1
        return result(self.states.pop(0))


class FakeSleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


@pytest.fixture
def fake_check(monkeypatch):
    def install(*states):
        check = FakeCheck(*states)
        monkeypatch.setattr(status, "HTTPCheck", check)
        return check

    return install


async def test_up_first_time_means_no_retry(fake_check):
    check, sleep = fake_check("up"), FakeSleep()
    assert (await run_with_retry(monitor(), sleep=sleep)).status == "up"
    assert (check.calls, sleep.delays) == (1, [])


async def test_retries_wait_the_retry_interval(fake_check):
    check, sleep = fake_check("down", "down", "down"), FakeSleep()
    assert (await run_with_retry(monitor(retry_count=2, retry_interval_seconds=30), sleep=sleep)).status == "down"
    assert check.calls == 3
    assert sleep.delays == [30, 30]


async def test_retrying_stops_at_the_first_success(fake_check):
    check, sleep = fake_check("down", "up", "down"), FakeSleep()
    assert (await run_with_retry(monitor(), sleep=sleep)).status == "up"
    assert check.calls == 2


async def test_no_retries_configured(fake_check):
    check, sleep = fake_check("down"), FakeSleep()
    await run_with_retry(monitor(retry_count=0), sleep=sleep)
    assert (check.calls, sleep.delays) == (1, [])


async def test_check_gets_parsed_codes_and_the_ssrf_setting(fake_check, monkeypatch):
    check = fake_check("up")
    monkeypatch.setattr(status, "get_settings", lambda: SimpleNamespace(monitor_allow_private_targets=False))
    await run_with_retry(monitor(expected_status_codes="[200, 204]"), sleep=FakeSleep())
    assert check.kwargs["expected_status_codes"] == [200, 204]
    assert check.kwargs["allow_private"] is False


@pytest.mark.parametrize(
    ("failures", "retry_count", "state", "current", "expected"),
    [
        (0, 0, "up", "down", "up"),
        (0, 0, "down", "pending", "down"),
        (0, 2, "down", "up", "up"),  # not enough consecutive failures yet
        (2, 2, "down", "up", "down"),
    ],
)
def test_resolve_status(failures, retry_count, state, current, expected):
    m = monitor(consecutive_failures=failures, retry_count=retry_count, current_status=current)
    assert resolve_status(m, result(state)) == expected
