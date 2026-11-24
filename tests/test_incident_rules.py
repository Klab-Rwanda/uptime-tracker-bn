import random
from datetime import UTC, datetime, timedelta

import pytest

from app.services.incident_rules import (
    DEFAULT_RULE,
    CheckResult,
    IncidentAction,
    IncidentRule,
    decide,
)

NONE, OPEN, RESOLVE = IncidentAction.NONE, IncidentAction.OPEN, IncidentAction.RESOLVE
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def hb(status: str, minute: float, **kwargs) -> CheckResult:
    return CheckResult(status=status, checked_at=T0 + timedelta(minutes=minute), **kwargs)


def seq(*statuses: str) -> list[CheckResult]:
    return [hb(status, i) for i, status in enumerate(statuses)]


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


# --- no incident open ---------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], NONE),
        (["up"], NONE),
        (["down"], NONE),  # the first failure alone never opens an incident
        (["up", "down"], NONE),
        (["down", "down"], OPEN),
        (["up", "down", "down"], OPEN),
        (["down", "down", "down"], OPEN),
        (["down", "down", "up"], NONE),
        (["down", "up", "down"], NONE),  # not consecutive
    ],
)
def test_when_no_incident_is_open(statuses, expected):
    assert decide(seq(*statuses), incident_open=False).action is expected


def test_default_is_two_failures_as_the_mvp_requires():
    # MVP definition of done: "Two consecutive failures create a DOWN incident"
    assert DEFAULT_RULE.failures_to_open == 2
    assert DEFAULT_RULE.successes_to_resolve == 1


# --- incident already open ----------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], NONE),
        (["down"], NONE),
        (["down", "down"], NONE),  # still down: never open a second incident
        (["up"], RESOLVE),
        (["up", "up"], RESOLVE),
        (["down", "down", "up"], RESOLVE),
        (["down", "up", "down"], NONE),
    ],
)
def test_when_an_incident_is_open(statuses, expected):
    assert decide(seq(*statuses), incident_open=True).action is expected


def test_full_timeline_opens_and_resolves_once_per_outage():
    statuses = ["up", "up", "down", "down", "down", "up", "up", "down", "down"]
    history: list[CheckResult] = []
    incident_open = False
    events: list[tuple[int, str]] = []

    for i, status in enumerate(statuses):
        history.append(hb(status, i))
        decision = decide(history, incident_open=incident_open)
        if decision.action is OPEN:
            incident_open = True
            events.append((i, "open"))
        elif decision.action is RESOLVE:
            incident_open = False
            events.append((i, "resolve"))

    assert events == [(3, "open"), (5, "resolve"), (8, "open")]


# --- when it really happened (Decision.since) ---------------------------------


def test_open_dates_the_incident_from_the_first_failure():
    # Detected at the 2nd failure (minute 3), but the outage began at minute 2
    decision = decide(seq("up", "up", "down", "down"), incident_open=False)
    assert decision.action is OPEN
    assert decision.since == at(2)


def test_open_after_a_longer_streak_still_uses_its_first_failure():
    decision = decide(seq("up", "down", "down", "down", "down"), incident_open=False, rule=IncidentRule(failures_to_open=4))
    assert decision.since == at(1)


def test_resolve_dates_the_recovery_from_the_first_success():
    rule = IncidentRule(successes_to_resolve=2)
    decision = decide(seq("down", "down", "up", "up"), incident_open=True, rule=rule)
    assert decision.action is RESOLVE
    assert decision.since == at(2)


def test_since_ignores_failures_cut_off_by_the_gap():
    results = [hb("down", 0), hb("down", 600), hb("down", 601)]
    rule = IncidentRule(max_gap=timedelta(minutes=5))
    assert decide(results, incident_open=False, rule=rule).since == at(600)


def test_since_skips_intermediate_attempts():
    results = [hb("down", 0, is_intermediate=True), hb("down", 1), hb("down", 11)]
    assert decide(results, incident_open=False).since == at(1)


def test_since_is_only_set_when_acting():
    assert decide(seq("down"), incident_open=False).since is None
    assert decide(seq("down"), incident_open=True).since is None


# --- custom rules -------------------------------------------------------------


def test_three_failures_to_open():
    rule = IncidentRule(failures_to_open=3)
    assert decide(seq("down", "down"), incident_open=False, rule=rule).action is NONE
    assert decide(seq("down", "down", "down"), incident_open=False, rule=rule).action is OPEN


def test_a_single_failure_can_open_when_configured():
    rule = IncidentRule(failures_to_open=1)
    assert decide(seq("down"), incident_open=False, rule=rule).action is OPEN


def test_two_successes_to_resolve():
    rule = IncidentRule(successes_to_resolve=2)
    assert decide(seq("down", "up"), incident_open=True, rule=rule).action is NONE
    assert decide(seq("down", "up", "up"), incident_open=True, rule=rule).action is RESOLVE
    assert decide(seq("up", "down", "up"), incident_open=True, rule=rule).action is NONE


def test_rule_validation():
    with pytest.raises(ValueError):
        IncidentRule(failures_to_open=0)
    with pytest.raises(ValueError):
        IncidentRule(successes_to_resolve=0)
    with pytest.raises(ValueError):
        IncidentRule(max_gap=timedelta(0))


def test_min_history():
    assert IncidentRule().min_history == 2
    assert IncidentRule(failures_to_open=3, successes_to_resolve=2).min_history == 3
    assert IncidentRule(failures_to_open=1, successes_to_resolve=4).min_history == 4


# --- a rule from the monitor's schedule (IncidentRule.for_monitor) ------------


def test_for_monitor_sets_max_gap_from_interval_and_retries():
    rule = IncidentRule.for_monitor(
        check_interval_seconds=60, retry_count=2, retry_interval_seconds=30
    )
    # one check = 60 s + 2 retries x 30 s = 120 s; allow 3 of them
    assert rule.max_gap == timedelta(seconds=360)
    assert rule.failures_to_open == 2


def test_for_monitor_without_retries():
    assert IncidentRule.for_monitor(check_interval_seconds=60).max_gap == timedelta(minutes=3)


def test_for_monitor_passes_thresholds_through():
    rule = IncidentRule.for_monitor(
        check_interval_seconds=60, failures_to_open=3, successes_to_resolve=2
    )
    assert (rule.failures_to_open, rule.successes_to_resolve) == (3, 2)


def test_for_monitor_breaks_a_streak_across_a_pause():
    rule = IncidentRule.for_monitor(check_interval_seconds=60)
    after_pause = [hb("down", 0), hb("down", 60)]  # paused for an hour in between
    back_to_back = [hb("down", 0), hb("down", 1)]
    assert decide(after_pause, incident_open=False, rule=rule).action is NONE
    assert decide(back_to_back, incident_open=False, rule=rule).action is OPEN


def test_for_monitor_tolerates_one_slow_check():
    rule = IncidentRule.for_monitor(check_interval_seconds=60)
    assert decide([hb("down", 0), hb("down", 2.5)], incident_open=False, rule=rule).action is OPEN


@pytest.mark.parametrize(
    "kwargs",
    [
        {"check_interval_seconds": 0},
        {"check_interval_seconds": 60, "retry_count": -1},
        {"check_interval_seconds": 60, "retry_interval_seconds": -5},
    ],
)
def test_for_monitor_validation(kwargs):
    with pytest.raises(ValueError):
        IncidentRule.for_monitor(**kwargs)


# --- statuses that don't count ------------------------------------------------


def test_paused_and_maintenance_results_do_not_break_a_streak():
    results = [hb("down", 0), hb("paused", 1), hb("maintenance", 2), hb("down", 3)]
    assert decide(results, incident_open=False).action is OPEN


def test_pending_results_never_open_or_resolve():
    assert decide(seq("pending", "pending"), incident_open=False).action is NONE
    assert decide(seq("down", "pending"), incident_open=True).action is NONE


def test_status_is_case_insensitive():
    assert decide(seq("DOWN", "Down"), incident_open=False).action is OPEN


def test_unknown_status_raises():
    with pytest.raises(ValueError):
        decide(seq("up", "exploded"), incident_open=False)


# --- monitor retries (the open question from C-13) -----------------------------


def retried_check(start: float) -> list[CheckResult]:
    """One failed check: two intermediate attempts, then the final failure."""
    return [
        hb("down", start, is_intermediate=True),
        hb("down", start + 0.5, is_intermediate=True),
        hb("down", start + 1),
    ]


def test_one_failed_check_with_retries_is_one_failure_by_default():
    assert decide(retried_check(0), incident_open=False).action is NONE


def test_two_failed_checks_with_retries_open_an_incident():
    results = retried_check(0) + retried_check(10)
    assert decide(results, incident_open=False).action is OPEN


def test_counting_every_attempt_is_a_one_line_change():
    rule = IncidentRule(count_intermediate_attempts=True)
    assert decide(retried_check(0), incident_open=False, rule=rule).action is OPEN


def test_a_success_after_failed_attempts_resolves():
    results = [hb("down", 0, is_intermediate=True), hb("up", 1)]
    assert decide(results, incident_open=True).action is RESOLVE


# --- gaps ---------------------------------------------------------------------


def test_old_failures_do_not_combine_with_a_new_one():
    results = [hb("down", 0), hb("down", 600)]
    rule = IncidentRule(max_gap=timedelta(minutes=5))
    assert decide(results, incident_open=False, rule=rule).action is NONE
    assert decide(results, incident_open=False).action is OPEN  # no limit: the old behaviour


def test_failures_inside_the_gap_still_count():
    rule = IncidentRule(max_gap=timedelta(minutes=5))
    assert decide([hb("down", 0), hb("down", 3)], incident_open=False, rule=rule).action is OPEN


def test_a_gap_exactly_at_the_limit_is_allowed():
    rule = IncidentRule(max_gap=timedelta(minutes=5))
    assert decide([hb("down", 0), hb("down", 5)], incident_open=False, rule=rule).action is OPEN


# --- input handling -----------------------------------------------------------


def test_order_of_results_does_not_matter():
    results = seq("up", "down", "down")
    shuffled = results[:]
    random.Random(7).shuffle(shuffled)
    assert decide(shuffled, incident_open=False) == decide(results, incident_open=False)


def test_input_is_not_modified():
    results = seq("down", "up", "down")
    before = list(results)
    decide(results, incident_open=False)
    assert results == before


def test_naive_datetimes_are_rejected():
    naive = CheckResult(status="down", checked_at=datetime(2026, 11, 20, 10, 0))
    with pytest.raises(ValueError):
        decide([naive], incident_open=False)


def test_accepts_any_iterable():
    assert decide((r for r in seq("down", "down")), incident_open=False).action is OPEN


# --- cause and reason ---------------------------------------------------------


def test_cause_comes_from_the_newest_failure():
    results = [hb("down", 0, message="HTTP 500"), hb("down", 1, message="HTTP 503 from API")]
    assert decide(results, incident_open=False).cause == "HTTP 503 from API"


def test_cause_falls_back_to_error_class_then_a_default():
    only_class = [hb("down", 0, error_class="timeout"), hb("down", 1, error_class="timeout")]
    assert decide(only_class, incident_open=False).cause == "timeout"
    assert decide(seq("down", "down"), incident_open=False).cause == "Check failed"
    blank = [hb("down", 0, message="  \n "), hb("down", 1, message="  ", error_class="dns")]
    assert decide(blank, incident_open=False).cause == "dns"


def test_cause_is_one_line_and_clipped():
    message = "first line\nsecond line " + "x" * 500
    results = [hb("down", 0, message=message), hb("down", 1, message=message)]
    cause = decide(results, incident_open=False).cause
    assert "\n" not in cause
    assert len(cause) <= 200


def test_cause_is_only_set_when_opening():
    assert decide(seq("down"), incident_open=False).cause is None
    assert decide(seq("up"), incident_open=True).cause is None


def test_every_decision_explains_itself():
    for statuses, open_ in [([], False), (["down", "down"], False), (["up"], True)]:
        assert decide(seq(*statuses), incident_open=open_).reason
