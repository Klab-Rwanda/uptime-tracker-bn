"""Where incident detection gets a monitor's recent check results from.

Production reads the heartbeats table (SqlCheckHistory); tests use the
in-memory version. Detection only depends on the CheckHistory interface.
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.heartbeat import Heartbeat
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


def _aware(value: datetime) -> datetime:
    # SQLite returns timestamps without a timezone; everything stored is UTC
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SqlCheckHistory:
    """Reads a monitor's newest heartbeats (uses heartbeats_monitor_checked_idx).

    Heartbeats hold the final result of each check (retries happen inside one
    check), so none are intermediate attempts.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recent_results(self, monitor_id: UUID, *, limit: int) -> list[CheckResult]:
        rows = await self._session.scalars(
            select(Heartbeat)
            .where(Heartbeat.monitor_id == monitor_id)
            .order_by(Heartbeat.checked_at.desc())
            .limit(limit)
        )
        return [
            CheckResult(
                status=row.status,
                checked_at=_aware(row.checked_at),
                message=row.error_message,
            )
            for row in rows
        ]
