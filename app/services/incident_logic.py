from enum import StrEnum

# Consecutive failed checks before a monitor gets an incident (MVP definition of done)
FAILURE_THRESHOLD = 2


class IncidentStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class IncidentEventType(StrEnum):
    OPENED = "opened"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    NOTE = "note"


class InvalidIncidentTransition(ValueError):
    """Raised for a status change that isn't allowed, e.g. acknowledging a resolved incident."""


# Which statuses each status may move to. RESOLVED is final.
_ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.OPEN: frozenset({IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED}),
    IncidentStatus.ACKNOWLEDGED: frozenset({IncidentStatus.RESOLVED}),
    IncidentStatus.RESOLVED: frozenset(),
}


def should_open_incident(consecutive_failures: int, has_active_incident: bool) -> bool:
    """True when a monitor has failed enough times and has no incident yet."""
    return consecutive_failures >= FAILURE_THRESHOLD and not has_active_incident


def should_resolve_incident(is_up: bool, has_active_incident: bool) -> bool:
    """True when a monitor with an incident has recovered."""
    return is_up and has_active_incident


def transition(current: str, target: str) -> IncidentStatus:
    """Validate a status change and return the new status.

    Raises InvalidIncidentTransition if the change isn't allowed, and
    ValueError for an unknown status.
    """
    current_status = IncidentStatus(current.lower())
    target_status = IncidentStatus(target.lower())
    if target_status not in _ALLOWED_TRANSITIONS[current_status]:
        raise InvalidIncidentTransition(
            f"Cannot change incident from {current_status} to {target_status}"
        )
    return target_status
