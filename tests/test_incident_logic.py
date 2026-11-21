import pytest

from app.services.incident_logic import (
    FAILURE_THRESHOLD,
    IncidentStatus,
    InvalidIncidentTransition,
    should_open_incident,
    should_resolve_incident,
    transition,
)


@pytest.mark.parametrize(
    ("failures", "has_active", "expected"),
    [
        (0, False, False),
        (1, False, False),
        (2, False, True),
        (5, False, True),
        (2, True, False),  # already has an incident: don't open a duplicate
        (5, True, False),
    ],
)
def test_should_open_incident(failures, has_active, expected):
    assert should_open_incident(failures, has_active) is expected


def test_failure_threshold_is_two():
    # The MVP definition of done: "Two consecutive failures create a DOWN incident"
    assert FAILURE_THRESHOLD == 2


@pytest.mark.parametrize(
    ("is_up", "has_active", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_should_resolve_incident(is_up, has_active, expected):
    assert should_resolve_incident(is_up, has_active) is expected


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("open", "acknowledged"),
        ("open", "resolved"),
        ("acknowledged", "resolved"),
    ],
)
def test_allowed_transitions(current, target):
    assert transition(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("open", "open"),
        ("acknowledged", "acknowledged"),
        ("acknowledged", "open"),
        ("resolved", "open"),
        ("resolved", "acknowledged"),
        ("resolved", "resolved"),
    ],
)
def test_forbidden_transitions(current, target):
    with pytest.raises(InvalidIncidentTransition):
        transition(current, target)


def test_transition_is_case_insensitive():
    assert transition("OPEN", "Resolved") == IncidentStatus.RESOLVED


def test_unknown_status_raises():
    with pytest.raises(ValueError):
        transition("open", "exploded")
