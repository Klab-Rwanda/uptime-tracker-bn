from enum import StrEnum

# When an incident opens or resolves is decided in app/services/incident_rules.py.
# This module only covers the incident's own status changes.


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
