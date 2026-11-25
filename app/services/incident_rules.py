from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.services.status_logic import MonitorStatus

MAX_CAUSE_LENGTH = 200
DEFAULT_CAUSE = "Check failed"
# for_monitor(): results more than this many check intervals apart don't form a streak
GAP_INTERVALS = 3


class IncidentAction(StrEnum):
    NONE = "none"
    OPEN = "open"
    RESOLVE = "resolve"


@dataclass(frozen=True)
class CheckResult:
    """One stored heartbeat, reduced to what the decision needs."""

    status: str  # "up" or "down" decide; pending/paused/maintenance are ignored
    checked_at: datetime  # must be timezone-aware
    message: str | None = None
    error_class: str | None = None
    region: str | None = None  # not used yet (see notes on multi-region)
    # True for a failed attempt that the monitor will retry. The final result of
    # that check arrives in a later heartbeat.
    is_intermediate: bool = False


@dataclass(frozen=True)
class IncidentRule:
    failures_to_open: int = 2
    successes_to_resolve: int = 1
    # False: only the final result of each check counts, so a monitor with
    # retries=2 needs two failed CHECKS, not two failed attempts.
    count_intermediate_attempts: bool = False
    # Results further apart than this don't belong to the same streak. Use
    # for_monitor() to set it from the monitor's schedule. None means no limit.
    max_gap: timedelta | None = None

    def __post_init__(self) -> None:
        if self.failures_to_open < 1:
            raise ValueError("failures_to_open must be at least 1")
        if self.successes_to_resolve < 1:
            raise ValueError("successes_to_resolve must be at least 1")
        if self.max_gap is not None and self.max_gap <= timedelta(0):
            raise ValueError("max_gap must be positive")

    @classmethod
    def for_monitor(
        cls,
        *,
        check_interval_seconds: int,
        retry_count: int = 0,
        retry_interval_seconds: int = 0,
        failures_to_open: int = 2,
        successes_to_resolve: int = 1,
    ) -> "IncidentRule":
        """The rule for one monitor, with max_gap derived from its schedule.

        Two final results of consecutive checks are at most one interval plus
        that check's retries apart. Allowing GAP_INTERVALS of that absorbs a slow
        or skipped check, but still breaks a streak across a pause.
        """
        if check_interval_seconds <= 0:
            raise ValueError("check_interval_seconds must be positive")
        if retry_count < 0 or retry_interval_seconds < 0:
            raise ValueError("retry settings must not be negative")
        one_check = check_interval_seconds + retry_count * retry_interval_seconds
        return cls(
            failures_to_open=failures_to_open,
            successes_to_resolve=successes_to_resolve,
            max_gap=timedelta(seconds=GAP_INTERVALS * one_check),
        )

    @property
    def min_history(self) -> int:
        """Fetch at least this many results. Fetch more (about 20) when intermediate
        attempts are stored, because they are filtered out."""
        return max(self.failures_to_open, self.successes_to_resolve)


DEFAULT_RULE = IncidentRule()


@dataclass(frozen=True)
class Decision:
    action: IncidentAction
    reason: str  # for logs and the incident timeline
    cause: str | None = None  # only set when opening
    # When it really happened: the first failure of the streak (OPEN) or the first
    # success after it (RESOLVE). Use it as the incident's started_at / resolved_at,
    # so downtime isn't overstated or understated by the detection delay.
    since: datetime | None = None


def _prepare(
    results: Iterable[CheckResult], rule: IncidentRule
) -> list[tuple[CheckResult, MonitorStatus]]:
    prepared: list[tuple[CheckResult, MonitorStatus]] = []
    for result in results:
        if result.checked_at.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware")
        # Raises ValueError for an unknown status, so a new status elsewhere is noticed
        status = MonitorStatus(result.status.lower())
        if status not in (MonitorStatus.UP, MonitorStatus.DOWN):
            continue
        if (
            status is MonitorStatus.DOWN
            and result.is_intermediate
            and not rule.count_intermediate_attempts
        ):
            continue
        prepared.append((result, status))
    prepared.sort(key=lambda pair: pair[0].checked_at)  # stable for equal timestamps
    return prepared


def _trailing_streak(
    prepared: list[tuple[CheckResult, MonitorStatus]],
    target: MonitorStatus,
    max_gap: timedelta | None,
) -> list[CheckResult]:
    """The unbroken run of `target` results at the end. The newest comes first."""
    streak: list[CheckResult] = []
    for result, status in reversed(prepared):
        if status is not target:
            break
        if streak and max_gap is not None and streak[-1].checked_at - result.checked_at > max_gap:
            break
        streak.append(result)
    return streak


def _cause(result: CheckResult) -> str:
    for text in (result.message, result.error_class):
        cleaned = " ".join((text or "").split())[:MAX_CAUSE_LENGTH]
        if cleaned:
            return cleaned
    return DEFAULT_CAUSE


def decide(
    results: Iterable[CheckResult],
    *,
    incident_open: bool,
    rule: IncidentRule = DEFAULT_RULE,
) -> Decision:
    """Decide what to do with a monitor's incident.

    `incident_open` is True when the monitor has an incident that is not resolved
    yet (open or acknowledged). Results may come in any order.
    """
    prepared = _prepare(results, rule)

    if incident_open:
        streak = _trailing_streak(prepared, MonitorStatus.UP, rule.max_gap)
        if len(streak) >= rule.successes_to_resolve:
            return Decision(
                IncidentAction.RESOLVE,
                f"{len(streak)} consecutive successful check(s)",
                since=streak[-1].checked_at,
            )
        return Decision(IncidentAction.NONE, "incident is open and the service has not recovered")

    streak = _trailing_streak(prepared, MonitorStatus.DOWN, rule.max_gap)
    if len(streak) >= rule.failures_to_open:
        return Decision(
            IncidentAction.OPEN,
            f"{len(streak)} consecutive failed check(s)",
            cause=_cause(streak[0]),
            since=streak[-1].checked_at,
        )
    return Decision(
        IncidentAction.NONE, f"{len(streak)} of {rule.failures_to_open} failures needed"
    )
