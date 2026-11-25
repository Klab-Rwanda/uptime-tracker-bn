"""Who may see and change incidents (used by the incident endpoints).

Any member of the organization may read its incidents; editors and above may
change them. An incident of an organization you don't belong to answers exactly
like a missing one, so nobody can probe which incidents exist elsewhere.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key_access import require_scope
from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.models.incident import Incident
from app.organizations.service import _get_org_membership

READ_SCOPE = "incidents:read"
WRITE_SCOPE = "incidents:write"
WRITE_ROLES = ("owner", "admin", "editor")


def incident_not_found() -> APIError:
    return APIError(404, "incident_not_found", "Incident not found")


async def load_incident(
    session: AsyncSession,
    incident_id: UUID,
    current: AuthenticatedUser,
    *,
    write: bool,
) -> Incident:
    """The incident, if `current` may read it (write=False) or change it (write=True)."""
    require_scope(current, WRITE_SCOPE if write else READ_SCOPE)
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise incident_not_found()
    try:
        _, membership = await _get_org_membership(
            session, organization_id=incident.organization_id, user_id=current.user.id
        )
    except APIError as exc:
        if exc.status_code == 404:  # not a member: answer like a missing incident
            raise incident_not_found() from None
        raise
    if write and membership.role not in WRITE_ROLES:
        raise APIError(403, "forbidden", "An editor role or higher is required to change an incident")
    return incident


async def require_member(
    session: AsyncSession, organization_id: UUID, current: AuthenticatedUser
) -> None:
    """For listing: any member of the organization may read its incidents."""
    require_scope(current, READ_SCOPE)
    await _get_org_membership(session, organization_id=organization_id, user_id=current.user.id)
