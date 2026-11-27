from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, scoped
from app.monitors import service
from app.monitors.schemas import (
    MonitorCreate,
    MonitorEnvelope,
    MonitorListEnvelope,
    MonitorStatusEnvelope,
    MonitorStatusResponse,
    MonitorTestEnvelope,
    MonitorTestResponse,
    MonitorUpdate,
)
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/monitors", tags=["monitors"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
MonitorsRead = Annotated[AuthenticatedUser, Depends(scoped("monitors:read"))]
MonitorsWrite = Annotated[AuthenticatedUser, Depends(scoped("monitors:write"))]


@router.post(
    "",
    response_model=MonitorEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def create_monitor(
    request: MonitorCreate,
    current: MonitorsWrite,
    session: DatabaseSession,
) -> MonitorEnvelope:
    monitor = await service.create_monitor(
        session,
        data=request,
        current=current,
    )
    return MonitorEnvelope(data=service.to_monitor_response(monitor))


@router.get("", response_model=MonitorListEnvelope)
async def list_monitors(
    organization_id: Annotated[UUID, Query(description="Organization ID to filter monitors")],
    current: MonitorsRead,
    session: DatabaseSession,
    project_id: Annotated[UUID | None, Query(description="Project ID to filter monitors")] = None,
    status: Annotated[str | None, Query(description="Status to filter monitors (e.g. pending, up, down, paused)")] = None,
    type: Annotated[str | None, Query(description="Monitor type to filter (e.g. http, tcp, dns, push)")] = None,
) -> MonitorListEnvelope:
    monitors = await service.list_monitors(
        session,
        organization_id=organization_id,
        project_id=project_id,
        status=status,
        monitor_type=type,
        current=current,
    )
    return MonitorListEnvelope(
        data=[service.to_monitor_response(m) for m in monitors]
    )


@router.get("/{monitor_id}", response_model=MonitorEnvelope)
async def get_monitor(
    monitor_id: UUID,
    current: MonitorsRead,
    session: DatabaseSession,
) -> MonitorEnvelope:
    monitor = await service.get_monitor(
        session,
        monitor_id=monitor_id,
        current=current,
    )
    return MonitorEnvelope(data=service.to_monitor_response(monitor))


@router.patch("/{monitor_id}", response_model=MonitorEnvelope)
async def update_monitor(
    monitor_id: UUID,
    request: MonitorUpdate,
    current: MonitorsWrite,
    session: DatabaseSession,
) -> MonitorEnvelope:
    monitor = await service.update_monitor(
        session,
        monitor_id=monitor_id,
        data=request,
        current=current,
    )
    return MonitorEnvelope(data=service.to_monitor_response(monitor))


@router.delete("/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_monitor(
    monitor_id: UUID,
    current: MonitorsWrite,
    session: DatabaseSession,
) -> Response:
    await service.delete_monitor(
        session,
        monitor_id=monitor_id,
        current=current,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{monitor_id}/pause", response_model=MonitorStatusEnvelope)
async def pause_monitor(
    monitor_id: UUID,
    current: MonitorsWrite,
    session: DatabaseSession,
) -> MonitorStatusEnvelope:
    monitor = await service.pause_monitor(
        session,
        monitor_id=monitor_id,
        current=current,
    )
    return MonitorStatusEnvelope(
        data=MonitorStatusResponse(
            id=monitor.id,
            status=monitor.current_status,
            is_active=monitor.is_active,
        )
    )


@router.post("/{monitor_id}/resume", response_model=MonitorStatusEnvelope)
async def resume_monitor(
    monitor_id: UUID,
    current: MonitorsWrite,
    session: DatabaseSession,
) -> MonitorStatusEnvelope:
    monitor = await service.resume_monitor(
        session,
        monitor_id=monitor_id,
        current=current,
    )
    return MonitorStatusEnvelope(
        data=MonitorStatusResponse(
            id=monitor.id,
            status=monitor.current_status,
            is_active=monitor.is_active,
        )
    )


@router.post("/{monitor_id}/test", response_model=MonitorTestEnvelope)
async def test_monitor(
    monitor_id: UUID,
    current: MonitorsWrite,
    session: DatabaseSession,
) -> MonitorTestEnvelope:
    result = await service.test_monitor(
        session,
        monitor_id=monitor_id,
        current=current,
    )
    return MonitorTestEnvelope(
        data=MonitorTestResponse(
            monitor_id=UUID(result["monitor_id"]),
            status=result["status"],
            message=result["message"],
        )
    )
