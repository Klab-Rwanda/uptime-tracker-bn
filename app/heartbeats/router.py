from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, scoped
from app.heartbeats import service
from app.heartbeats.schemas import (
    HeartbeatListEnvelope,
    HeartbeatListMeta,
    LatestHeartbeatEnvelope,
)
from app.heartbeats.service import HeartbeatStatus
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/monitors", tags=["heartbeats"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
MonitorsRead = Annotated[AuthenticatedUser, Depends(scoped("monitors:read"))]


@router.get("/{monitor_id}/heartbeats", response_model=HeartbeatListEnvelope)
async def list_heartbeats(
    monitor_id: UUID,
    current: MonitorsRead,
    session: DatabaseSession,
    status: Annotated[HeartbeatStatus | None, Query(description="Only up, down or pending")] = None,
    since: Annotated[AwareDatetime | None, Query(description="Checked at or after")] = None,
    until: Annotated[AwareDatetime | None, Query(description="Checked before")] = None,
    cursor: Annotated[
        str | None, Query(max_length=200, description="meta.next_cursor of the previous page")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = service.DEFAULT_LIMIT,
) -> HeartbeatListEnvelope:
    """The monitor's check results, newest first."""
    heartbeats, next_cursor = await service.list_heartbeats(
        session,
        monitor_id=monitor_id,
        current=current,
        status=status,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
    )
    return HeartbeatListEnvelope(data=heartbeats, meta=HeartbeatListMeta(next_cursor=next_cursor))


@router.get("/{monitor_id}/heartbeats/latest", response_model=LatestHeartbeatEnvelope)
async def latest_heartbeat(
    monitor_id: UUID, current: MonitorsRead, session: DatabaseSession
) -> LatestHeartbeatEnvelope:
    """The most recent check result, or null before the first check."""
    heartbeat = await service.latest_heartbeat(session, monitor_id=monitor_id, current=current)
    return LatestHeartbeatEnvelope(data=heartbeat)
