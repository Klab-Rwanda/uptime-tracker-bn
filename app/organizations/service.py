import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.auth.models import User
from app.notifications.brevo import EmailDeliveryError, send_email
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
)
from app.util.config import get_settings

OWNER_ROLE = "owner"
ADMIN_ROLE = "admin"
EDITOR_ROLE = "editor"
VIEWER_ROLE = "viewer"
MEMBER_ROLE = "member"

INVITATION_TTL = timedelta(hours=48)
INVITATION_TOKEN_BYTES = 32
MAX_ORG_INVITATIONS_PER_HOUR = 20
MAX_RECIPIENT_INVITATIONS_PER_DAY = 10

logger = logging.getLogger(__name__)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _invitation_status(invitation: OrganizationInvitation, now: datetime) -> str:
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "revoked"
    if _utc_datetime(invitation.expires_at) <= now:
        return "expired"
    return "pending"


async def create_organization(
    session: AsyncSession,
    *,
    name: str,
    slug: str | None = None,
    owner: User,
) -> tuple[Organization, OrganizationMembership]:
    if slug is not None:
        existing = await session.scalar(
            select(Organization).where(Organization.slug == slug)
        )
        if existing is not None:
            raise APIError(409, "conflict", "An organization with this slug already exists")

    organization = Organization(name=name, slug=slug)
    session.add(organization)
    await session.flush()

    membership = OrganizationMembership(
        organization_id=organization.id,
        user_id=owner.id,
        role=OWNER_ROLE,
    )
    session.add(membership)
    await session.commit()
    await session.refresh(organization)
    await session.refresh(membership)
    return organization, membership


async def list_organizations(
    session: AsyncSession,
    user_id: UUID,
) -> list[tuple[Organization, OrganizationMembership]]:
    stmt = (
        select(Organization, OrganizationMembership)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(OrganizationMembership.user_id == user_id)
        .order_by(Organization.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.all())


async def _get_org_membership(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
) -> tuple[Organization, OrganizationMembership]:
    stmt = (
        select(Organization, OrganizationMembership)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(
            Organization.id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise APIError(404, "organization_not_found", "Organization not found")
    return row[0], row[1]


async def get_organization(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> tuple[Organization, OrganizationMembership]:
    return await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )


async def update_organization(
    session: AsyncSession,
    *,
    organization_id: UUID,
    name: str,
    current: AuthenticatedUser,
) -> tuple[Organization, OrganizationMembership]:
    organization, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if membership.role not in (OWNER_ROLE, ADMIN_ROLE):
        raise APIError(
            403,
            "forbidden",
            "Only an owner or administrator can update the organization",
        )
    organization.name = name
    await session.commit()
    await session.refresh(organization)
    return organization, membership


async def list_members(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> list[dict]:
    await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    stmt = (
        select(
            OrganizationMembership.user_id,
            User.email,
            User.full_name,
            OrganizationMembership.role,
            OrganizationMembership.created_at,
        )
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.organization_id == organization_id)
        .order_by(OrganizationMembership.created_at.asc())
    )
    result = await session.execute(stmt)
    members = []
    for row in result.all():
        members.append(
            {
                "user_id": row[0],
                "email": row[1],
                "full_name": row[2],
                "role": row[3],
                "created_at": row[4],
            }
        )
    return members


async def remove_member(
    session: AsyncSession,
    *,
    organization_id: UUID,
    member_user_id: UUID,
    current: AuthenticatedUser,
) -> None:
    _, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if membership.role not in (OWNER_ROLE, ADMIN_ROLE):
        raise APIError(
            403,
            "forbidden",
            "Only an owner or administrator can remove members",
        )

    target_membership = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == member_user_id,
        )
    )
    if target_membership is None:
        raise APIError(404, "member_not_found", "Organization member not found")

    if target_membership.role == OWNER_ROLE:
        raise APIError(
            400,
            "cannot_remove_owner",
            "The organization owner cannot be removed",
        )

    await session.delete(target_membership)
    await session.commit()


async def leave_organization(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> None:
    _, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if membership.role == OWNER_ROLE:
        raise APIError(
            409,
            "owner_cannot_leave",
            "Transfer ownership before leaving this organization",
        )

    await session.delete(membership)
    await session.commit()


def _require_owner(membership: OrganizationMembership) -> None:
    if membership.role != OWNER_ROLE:
        raise APIError(
            403,
            "forbidden",
            "Only the organization owner can perform this action",
        )


async def transfer_ownership(
    session: AsyncSession,
    *,
    organization_id: UUID,
    target_user_id: UUID,
    current: AuthenticatedUser,
) -> None:
    _, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    _require_owner(membership)

    if current.user.id == target_user_id:
        raise APIError(
            400,
            "already_owner",
            "This account is already the organization owner",
        )

    target_membership = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == target_user_id,
        )
    )
    if target_membership is None:
        raise APIError(
            404,
            "member_not_found",
            "The new owner must already be an organization member",
        )

    membership.role = ADMIN_ROLE
    await session.flush()
    target_membership.role = OWNER_ROLE
    await session.commit()


async def create_invitation(
    session: AsyncSession,
    *,
    organization_id: UUID,
    recipient_email: str,
    role: str = EDITOR_ROLE,
    current: AuthenticatedUser,
) -> tuple[OrganizationInvitation, str]:
    organization, inviter_membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if inviter_membership.role not in (OWNER_ROLE, ADMIN_ROLE):
        raise APIError(
            403,
            "forbidden",
            "Only an owner or administrator can invite collaborators",
        )

    recipient = await session.scalar(
        select(User).where(
            User.email == recipient_email,
            User.is_active.is_(True),
            User.is_email_verified.is_(True),
        )
    )
    if recipient is None:
        raise APIError(
            404,
            "verified_account_required",
            "An active account with a verified email is required before inviting a collaborator",
        )

    existing_membership = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == recipient.id,
        )
    )
    if existing_membership is not None:
        raise APIError(
            409,
            "already_a_member",
            "This account is already a member of the organization",
        )

    now = datetime.now(UTC)
    pending = await session.scalar(
        select(OrganizationInvitation).where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.email == recipient_email,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
            OrganizationInvitation.expires_at > now,
        )
    )
    if pending is not None:
        raise APIError(
            409,
            "invitation_already_pending",
            "An invitation is already pending for this account",
        )

    org_window_start = now - timedelta(hours=1)
    org_invitation_count = await session.scalar(
        select(func.count(OrganizationInvitation.id)).where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.created_at >= org_window_start,
        )
    ) or 0
    if org_invitation_count >= MAX_ORG_INVITATIONS_PER_HOUR:
        raise APIError(
            429,
            "invitation_rate_limited",
            "The organization has sent too many invitations. Try again later.",
        )

    recipient_window_start = now - timedelta(days=1)
    recipient_invitation_count = await session.scalar(
        select(func.count(OrganizationInvitation.id)).where(
            OrganizationInvitation.email == recipient_email,
            OrganizationInvitation.created_at >= recipient_window_start,
        )
    ) or 0
    if recipient_invitation_count >= MAX_RECIPIENT_INVITATIONS_PER_DAY:
        raise APIError(
            429,
            "invitation_rate_limited",
            "Too many invitations have been sent to this account. Try again later.",
        )

    token = secrets.token_urlsafe(INVITATION_TOKEN_BYTES)
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    expires_at = now + INVITATION_TTL

    invitation = OrganizationInvitation(
        organization_id=organization_id,
        email=recipient_email,
        role=role,
        invited_by_user_id=current.user.id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(invitation)
    await session.commit()
    await session.refresh(invitation)

    settings = get_settings()
    invite_url = (
        f"{settings.app_frontend_url.rstrip('/')}/invitations/accept#token={quote(token)}"
    )
    try:
        await send_email(
            "invite",
            recipient=recipient_email,
            parameters={
                "to_name": recipient.full_name,
                "inviter_name": current.user.full_name,
                "organization_name": organization.name,
                "role": role,
                "invite_url": invite_url,
            },
        )
    except EmailDeliveryError as error:
        logger.warning(
            "Organization invitation email delivery failed for invitation %s: %s",
            invitation.id,
            error,
        )

    return invitation, "pending"


async def list_invitations(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> list[tuple[OrganizationInvitation, str]]:
    _, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if membership.role not in (OWNER_ROLE, ADMIN_ROLE):
        raise APIError(
            403,
            "forbidden",
            "Only an owner or administrator can view invitations",
        )

    stmt = (
        select(OrganizationInvitation)
        .where(OrganizationInvitation.organization_id == organization_id)
        .order_by(OrganizationInvitation.created_at.desc())
    )
    result = await session.execute(stmt)
    invitations = list(result.scalars().all())
    now = datetime.now(UTC)
    return [(inv, _invitation_status(inv, now)) for inv in invitations]


async def revoke_invitation(
    session: AsyncSession,
    *,
    organization_id: UUID,
    invitation_id: UUID,
    current: AuthenticatedUser,
) -> None:
    _, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if membership.role not in (OWNER_ROLE, ADMIN_ROLE):
        raise APIError(
            403,
            "forbidden",
            "Only an owner or administrator can revoke invitations",
        )

    invitation = await session.scalar(
        select(OrganizationInvitation).where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.organization_id == organization_id,
        )
    )
    if invitation is None:
        raise APIError(404, "invitation_not_found", "Invitation not found")

    now = datetime.now(UTC)
    status = _invitation_status(invitation, now)
    if status != "pending":
        raise APIError(
            400,
            "invalid_invitation_state",
            f"Cannot revoke an invitation that is already {status}",
        )

    invitation.revoked_at = now
    await session.commit()


async def accept_invitation(
    session: AsyncSession,
    *,
    token: str,
    current: AuthenticatedUser,
) -> tuple[Organization, str]:
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    invitation = await session.scalar(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.token_hash == token_hash,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if invitation is None:
        raise APIError(
            400,
            "invalid_invitation",
            "The invitation is invalid or expired",
        )

    now = datetime.now(UTC)
    if _utc_datetime(invitation.expires_at) <= now:
        await session.commit()
        raise APIError(
            410,
            "invitation_expired",
            "The invitation has expired",
        )

    if not current.user.is_active or not current.user.is_email_verified:
        raise APIError(
            403,
            "verified_account_required",
            "A verified account is required to accept an invitation",
        )

    if current.user.email != invitation.email:
        raise APIError(
            403,
            "invitation_email_mismatch",
            "Sign in with the account this invitation was sent to",
        )

    existing_membership = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == invitation.organization_id,
            OrganizationMembership.user_id == current.user.id,
        )
    )
    if existing_membership is not None:
        raise APIError(
            409,
            "already_a_member",
            "This account is already a member of the organization",
        )

    organization = await session.get(Organization, invitation.organization_id)
    if organization is None:
        raise APIError(404, "organization_not_found", "Organization not found")

    membership = OrganizationMembership(
        organization_id=invitation.organization_id,
        user_id=current.user.id,
        role=invitation.role,
    )
    session.add(membership)
    invitation.accepted_at = now
    await session.commit()
    return organization, invitation.role
