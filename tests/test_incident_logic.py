import pytest

from app.services.incident_logic import (
    IncidentStatus,
    InvalidIncidentTransition,
    transition,
)

# When incidents open and resolve is tested in tests/test_incident_rules.py


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
