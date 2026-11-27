from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service
from app.audit.schemas import AuditLogListEnvelope
from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/audit-logs", tags=["audit-logs"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]


@router.get("", response_model=AuditLogListEnvelope)
async def list_audit_logs(
    organization_id: Annotated[UUID, Query(description="Organization ID")],
    current: CurrentAuth,
    session: DatabaseSession,
    action: Annotated[
        str | None, Query(max_length=64, description='e.g. "status_page.deleted"')
    ] = None,
    target_id: Annotated[UUID | None, Query(description="Only entries about this object")] = None,
    before: Annotated[
        AwareDatetime | None, Query(description="next_before of the previous page")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = service.DEFAULT_LIMIT,
) -> AuditLogListEnvelope:
    entries, next_before = await service.list_logs(
        session,
        organization_id=organization_id,
        current=current,
        action=action,
        target_id=target_id,
        before=before,
        limit=limit,
    )
    return AuditLogListEnvelope(data=entries, next_before=next_before)

