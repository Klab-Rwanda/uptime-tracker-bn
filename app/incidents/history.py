"""Where incident detection gets a monitor's recent check results from.

Heartbeats (Developer B) don't exist yet, so detection depends only on this
interface. When the heartbeat table lands, add a class here that reads it and
returns CheckResult objects; nothing else has to change.
"""

from collections import defaultdict
from typing import Protocol
from uuid import UUID

from app.services.incident_rules import CheckResult

# Enough to cover the rule's streaks even when intermediate retry attempts are
# stored as heartbeats and filtered out
DEFAULT_HISTORY_LIMIT = 20


class CheckHistory(Protocol):
    async def recent_results(self, monitor_id: UUID, *, limit: int) -> list[CheckResult]:
        """The monitor's newest `limit` results, in any order."""
        ...


class InMemoryCheckHistory:
    """For tests and local runs."""

    def __init__(self) -> None:
        self._results: dict[UUID, list[CheckResult]] = defaultdict(list)

    def add(self, monitor_id: UUID, result: CheckResult) -> None:
        self._results[monitor_id].append(result)

    async def recent_results(self, monitor_id: UUID, *, limit: int) -> list[CheckResult]:
        newest_first = sorted(
            self._results[monitor_id], key=lambda r: r.checked_at, reverse=True
        )
        return newest_first[:limit]
