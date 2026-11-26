from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.maintenance import service
from app.maintenance.schemas import (
    MaintenanceWindowCreate,
    MaintenanceWindowEnvelope,
    MaintenanceWindowListEnvelope,
    MaintenanceWindowUpdate,
    WindowState,
)
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/maintenance-windows", tags=["maintenance-windows"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]


@router.post("", response_model=MaintenanceWindowEnvelope, status_code=status.HTTP_201_CREATED)
async def create_maintenance_window(
    request: MaintenanceWindowCreate, current: CurrentAuth, session: DatabaseSession
) -> MaintenanceWindowEnvelope:
    window = await service.create_window(session, data=request, current=current)
    return MaintenanceWindowEnvelope(data=window)


@router.get("", response_model=MaintenanceWindowListEnvelope)
async def list_maintenance_windows(
    organization_id: Annotated[UUID, Query(description="Organization ID")],
    current: CurrentAuth,
    session: DatabaseSession,
    state: Annotated[WindowState | None, Query(description="Only upcoming, active or past")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = service.DEFAULT_LIMIT,
) -> MaintenanceWindowListEnvelope:
    windows = await service.list_windows(
        session, organization_id=organization_id, current=current, state=state, limit=limit
    )
    return MaintenanceWindowListEnvelope(data=windows)


@router.patch("/{window_id}", response_model=MaintenanceWindowEnvelope)
async def update_maintenance_window(
    window_id: UUID, request: MaintenanceWindowUpdate, current: CurrentAuth, session: DatabaseSession
) -> MaintenanceWindowEnvelope:
    window = await service.update_window(
        session, window_id=window_id, data=request, current=current
    )
    return MaintenanceWindowEnvelope(data=window)


@router.delete("/{window_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_maintenance_window(
    window_id: UUID, current: CurrentAuth, session: DatabaseSession
) -> None:
    await service.delete_window(session, window_id=window_id, current=current)
