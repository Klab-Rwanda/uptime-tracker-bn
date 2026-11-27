"""Audit logs: a record of important changes, for organization owners and admins.

Recording a change (from any service, before its commit):

    from app.audit.service import record

    record(session, current, organization_id=org_id, action="monitor.deleted",
           target_type="monitor", target_id=monitor.id, details={"name": monitor.name})
    await session.commit()

`details` must never contain secrets, URLs or configs: names and the names of
changed fields are enough to understand what happened.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditLog
from app.audit.schemas import AuditActor, AuditLogResponse
from app.auth.api_key_access import require_scope
from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.auth.models import User
from app.organizations.service import _get_org_membership
from app.util.logs import request_id_ctx

READ_SCOPE = "audit_logs:read"
READ_ROLES = ("owner", "admin")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def record(
    session: AsyncSession,
    current: AuthenticatedUser,
    *,
    organization_id: UUID,
    action: str,
    target_type: str,
    target_id: UUID | None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit entry to the session; it is saved by the caller's commit."""
    session.add(
        AuditLog(
            organization_id=organization_id,
            actor_user_id=current.user.id,
            actor_api_key_id=current.api_key.key_id if current.api_key else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
            request_id=request_id_ctx.get(),
        )
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def list_logs(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
    action: str | None = None,
    target_id: UUID | None = None,
    before: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[AuditLogResponse], datetime | None]:
    """Newest first. Owners and admins only: the log shows everyone's actions."""
    require_scope(current, READ_SCOPE)
    _, membership = await _get_org_membership(
        session, organization_id=organization_id, user_id=current.user.id
    )
    if membership.role not in READ_ROLES:
        raise APIError(
            403, "forbidden", "An owner or administrator role is required to read audit logs"
        )

    stmt = (
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .where(AuditLog.organization_id == organization_id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if before is not None:
        stmt = stmt.where(AuditLog.created_at < _aware(before))
    # One extra row tells whether there is another page
    rows = (
        await session.execute(
            stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1)
        )
    ).all()

    entries = [
        AuditLogResponse(
            id=log.id,
            action=log.action,
            target_type=log.target_type,
            target_id=log.target_id,
            actor=AuditActor(
                user_id=log.actor_user_id, email=email, api_key_id=log.actor_api_key_id
            ),
            details=log.details,
            request_id=log.request_id,
            created_at=_aware(log.created_at),
        )
        for log, email in rows[:limit]
    ]
    next_before = entries[-1].created_at if len(rows) > limit else None
    return entries, next_before
