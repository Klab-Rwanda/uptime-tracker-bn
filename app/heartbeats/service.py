"""Reading a monitor's check history (heartbeats), newest first.

Pages use a cursor: the position (checked_at, id) of the last heartbeat
returned. The next page starts strictly after it, so heartbeats with the same
timestamp are never skipped or repeated.
"""

import base64
import json
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.heartbeats.schemas import HeartbeatResponse
from app.models.heartbeat import Heartbeat
from app.models.monitor import Monitor
from app.organizations.service import _get_org_membership

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class HeartbeatStatus(StrEnum):
    UP = "up"
    DOWN = "down"
    PENDING = "pending"


def _aware(value: datetime) -> datetime:
    """SQLite gives back naive datetimes; everything is stored in UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def encode_cursor(heartbeat: Heartbeat) -> str:
    raw = json.dumps({"t": _aware(heartbeat.checked_at).isoformat(), "id": str(heartbeat.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return _aware(datetime.fromisoformat(data["t"])), UUID(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise APIError(422, "validation_error", "The cursor is not valid") from exc


def to_response(heartbeat: Heartbeat) -> HeartbeatResponse:
    return HeartbeatResponse(
        id=heartbeat.id,
        monitor_id=heartbeat.monitor_id,
        status=heartbeat.status,
        response_time_ms=heartbeat.response_time_ms,
        message=heartbeat.error_message,
        checked_at=_aware(heartbeat.checked_at),
    )


async def _monitor_for(
    session: AsyncSession, monitor_id: UUID, current: AuthenticatedUser
) -> Monitor:
    """The monitor, if `current` belongs to its organization. Anyone else gets
    the same 404 as for a monitor that doesn't exist."""
    monitor = await session.get(Monitor, monitor_id)
    not_found = APIError(404, "monitor_not_found", "Monitor not found")
    if monitor is None:
        raise not_found
    try:
        await _get_org_membership(
            session, organization_id=monitor.organization_id, user_id=current.user.id
        )
    except APIError as exc:
        if exc.status_code == 404:
            raise not_found from None
        raise
    return monitor


async def list_heartbeats(
    session: AsyncSession,
    *,
    monitor_id: UUID,
    current: AuthenticatedUser,
    status: HeartbeatStatus | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[HeartbeatResponse], str | None]:
    position = decode_cursor(cursor) if cursor else None  # bad input before any lookup
    monitor = await _monitor_for(session, monitor_id, current)

    stmt = select(Heartbeat).where(Heartbeat.monitor_id == monitor.id)
    if status is not None:
        stmt = stmt.where(Heartbeat.status == status.value)
    if since is not None:
        stmt = stmt.where(Heartbeat.checked_at >= _aware(since))
    if until is not None:
        stmt = stmt.where(Heartbeat.checked_at < _aware(until))
    if position is not None:
        checked_at, last_id = position
        stmt = stmt.where(
            or_(
                Heartbeat.checked_at < checked_at,
                and_(Heartbeat.checked_at == checked_at, Heartbeat.id < last_id),
            )
        )
    # One extra row tells whether there is another page
    rows = list(
        (
            await session.scalars(
                stmt.order_by(Heartbeat.checked_at.desc(), Heartbeat.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]) if len(rows) > limit else None
    return [to_response(h) for h in page], next_cursor


async def latest_heartbeat(
    session: AsyncSession, *, monitor_id: UUID, current: AuthenticatedUser
) -> HeartbeatResponse | None:
    monitor = await _monitor_for(session, monitor_id, current)
    heartbeat = await session.scalar(
        select(Heartbeat)
        .where(Heartbeat.monitor_id == monitor.id)
        .order_by(Heartbeat.checked_at.desc(), Heartbeat.id.desc())
        .limit(1)
    )
    return None if heartbeat is None else to_response(heartbeat)
