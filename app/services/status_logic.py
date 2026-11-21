from collections.abc import Iterable
from enum import StrEnum


class MonitorStatus(StrEnum):
    UP = "up"
    PENDING = "pending"
    DOWN = "down"
    MAINTENANCE = "maintenance"
    PAUSED = "paused"


class OverallStatus(StrEnum):
    OPERATIONAL = "operational"
    PARTIAL_OUTAGE = "partial_outage"
    MAJOR_OUTAGE = "major_outage"
    MAINTENANCE = "maintenance"


def calculate_overall_status(statuses: Iterable[str]) -> OverallStatus:
    """Turn a collection of monitor statuses into one headline status.

    Works for a whole status page or for a single group on it.
    Statuses are case-insensitive ("UP" and "up" are the same).
    PAUSED and PENDING monitors are ignored.
    Raises ValueError for a status it doesn't know, so a new status added
    elsewhere is noticed in tests instead of being silently miscounted.
    """
    counts = {status: 0 for status in MonitorStatus}
    for raw in statuses:
        counts[MonitorStatus(raw.lower())] += 1

    down = counts[MonitorStatus.DOWN]
    up = counts[MonitorStatus.UP]
    maintenance = counts[MonitorStatus.MAINTENANCE]

    if down:
        if up == 0 and maintenance == 0:
            return OverallStatus.MAJOR_OUTAGE
        return OverallStatus.PARTIAL_OUTAGE
    if maintenance:
        return OverallStatus.MAINTENANCE
    return OverallStatus.OPERATIONAL


def effective_status(current_status: str, is_active: bool) -> MonitorStatus:
    """The status to show for one monitor.

    Pausing a monitor sets is_active=False but leaves current_status at its
    last value, so a monitor paused while "down" would otherwise keep
    reporting an outage nobody is checking anymore.
    """
    if not is_active:
        return MonitorStatus.PAUSED
    return MonitorStatus(current_status.lower())
