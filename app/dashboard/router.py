from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, scoped
from app.dashboard import service
from app.dashboard.schemas import DashboardSummaryEnvelope
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
MonitorsRead = Annotated[AuthenticatedUser, Depends(scoped("monitors:read"))]


@router.get("/summary", response_model=DashboardSummaryEnvelope)
async def get_dashboard_summary(
    organization_id: Annotated[UUID, Query(description="Organization ID for dashboard summary")],
    current: MonitorsRead,
    session: DatabaseSession,
) -> DashboardSummaryEnvelope:
    summary = await service.get_dashboard_summary(
        session,
        organization_id=organization_id,
        current=current,
    )
    return DashboardSummaryEnvelope(data=summary)
