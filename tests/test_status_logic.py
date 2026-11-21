import pytest

from app.services.status_logic import (
    MonitorStatus,
    OverallStatus,
    calculate_overall_status,
    effective_status,
)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "operational"),
        (["up"], "operational"),
        (["up", "up", "up"], "operational"),
        (["up", "down"], "partial_outage"),
        (["up", "up", "down"], "partial_outage"),
        (["down"], "major_outage"),
        (["down", "down"], "major_outage"),
        (["up", "maintenance"], "maintenance"),
        (["maintenance"], "maintenance"),
        (["down", "maintenance"], "partial_outage"),
        (["pending"], "operational"),
        (["pending", "up"], "operational"),
        (["paused"], "operational"),
        (["paused", "down"], "major_outage"),
        (["paused", "up", "down"], "partial_outage"),
        (["pending", "down"], "major_outage"),
    ],
)
def test_overall_status(statuses, expected):
    assert calculate_overall_status(statuses) == expected


def test_status_values_are_case_insensitive():
    assert calculate_overall_status(["UP", "Down"]) == OverallStatus.PARTIAL_OUTAGE


def test_accepts_any_iterable():
    assert calculate_overall_status(s for s in ["up", "down"]) == "partial_outage"


def test_unknown_status_raises():
    with pytest.raises(ValueError):
        calculate_overall_status(["up", "exploded"])


def test_effective_status_inactive_is_paused():
    assert effective_status("down", is_active=False) == MonitorStatus.PAUSED


def test_effective_status_active_keeps_current_status():
    assert effective_status("Down", is_active=True) == MonitorStatus.DOWN


def test_effective_status_unknown_raises():
    with pytest.raises(ValueError):
        effective_status("exploded", is_active=True)


def test_paused_monitor_that_was_down_does_not_cause_outage():
    monitors = [("up", True), ("down", False)]
    statuses = [effective_status(s, active) for s, active in monitors]
    assert calculate_overall_status(statuses) == OverallStatus.OPERATIONAL
