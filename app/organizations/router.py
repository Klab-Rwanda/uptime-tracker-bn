from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    AuthenticatedUser,
    get_user_login,
    scoped,
)
from app.organizations import service
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
)
from app.organizations.schemas import (
    AcceptInvitationEnvelope,
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    InvitationCreate,
    InvitationEnvelope,
    InvitationListEnvelope,
    InvitationResponse,
    MemberListEnvelope,
    MemberResponse,
    MessageEnvelope,
    OrganizationCreate,
    OrganizationEnvelope,
    OrganizationListEnvelope,
    OrganizationResponse,
    OrganizationUpdate,
    TransferOwnershipRequest,
)
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/organizations", tags=["organizations"])
invitations_router = APIRouter(prefix="/v1/invitations", tags=["invitations"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
OrganizationsRead = Annotated[AuthenticatedUser, Depends(scoped("organizations:read"))]
UserLogin = Annotated[AuthenticatedUser, Depends(get_user_login)]


@invitations_router.post(
    "/accept",
    response_model=AcceptInvitationEnvelope,
)
async def accept_invitation_direct(
    request: AcceptInvitationRequest,
    current: UserLogin,
    session: DatabaseSession,
) -> AcceptInvitationEnvelope:
    organization, role = await service.accept_invitation(
        session,
        token=request.token,
        current=current,
    )
    return AcceptInvitationEnvelope(
        data=AcceptInvitationResponse(
            organization_id=organization.id,
            organization_name=organization.name,
            role=role,
        )
    )


def _organization_response(
    organization: Organization,
    membership: OrganizationMembership,
) -> OrganizationResponse:
    return OrganizationResponse(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        role=membership.role,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
    )


def _invitation_response(
    invitation: OrganizationInvitation,
    invitation_status: str,
) -> InvitationResponse:
    return InvitationResponse(
        id=invitation.id,
        organization_id=invitation.organization_id,
        email=invitation.email,
        role=invitation.role,
        status=invitation_status,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
    )


@router.post(
    "",
    response_model=OrganizationEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    request: OrganizationCreate,
    current: UserLogin,
    session: DatabaseSession,
) -> OrganizationEnvelope:
    organization, membership = await service.create_organization(
        session,
        name=request.name,
        slug=request.slug,
        owner=current.user,
    )
    return OrganizationEnvelope(
        data=_organization_response(organization, membership)
    )


@router.get("", response_model=OrganizationListEnvelope)
async def list_organizations(
    current: OrganizationsRead,
    session: DatabaseSession,
) -> OrganizationListEnvelope:
    org_pairs = await service.list_organizations(session, current.user.id)
    if current.api_key is not None:
        # The key's creator may belong to other organizations: never list them
        org_pairs = [
            (org, membership)
            for org, membership in org_pairs
            if org.id == current.api_key.organization_id
        ]
    return OrganizationListEnvelope(
        data=[
            _organization_response(org, membership)
            for org, membership in org_pairs
        ]
    )


@router.post(
    "/invitations/accept",
    response_model=AcceptInvitationEnvelope,
)
async def accept_invitation(
    request: AcceptInvitationRequest,
    current: UserLogin,
    session: DatabaseSession,
) -> AcceptInvitationEnvelope:
    organization, role = await service.accept_invitation(
        session,
        token=request.token,
        current=current,
    )
    return AcceptInvitationEnvelope(
        data=AcceptInvitationResponse(
            organization_id=organization.id,
            organization_name=organization.name,
            role=role,
        )
    )


@router.get(
    "/{organization_id}",
    response_model=OrganizationEnvelope,
)
async def get_organization(
    organization_id: UUID,
    current: OrganizationsRead,
    session: DatabaseSession,
) -> OrganizationEnvelope:
    organization, membership = await service.get_organization(
        session,
        organization_id=organization_id,
        current=current,
    )
    return OrganizationEnvelope(
        data=_organization_response(organization, membership)
    )


@router.patch(
    "/{organization_id}",
    response_model=OrganizationEnvelope,
)
async def update_organization(
    organization_id: UUID,
    request: OrganizationUpdate,
    current: UserLogin,
    session: DatabaseSession,
) -> OrganizationEnvelope:
    organization, membership = await service.update_organization(
        session,
        organization_id=organization_id,
        name=request.name,
        current=current,
    )
    return OrganizationEnvelope(
        data=_organization_response(organization, membership)
    )


@router.post(
    "/{organization_id}/transfer-ownership",
    response_model=MessageEnvelope,
)
async def transfer_ownership(
    organization_id: UUID,
    request: TransferOwnershipRequest,
    current: UserLogin,
    session: DatabaseSession,
) -> MessageEnvelope:
    await service.transfer_ownership(
        session,
        organization_id=organization_id,
        target_user_id=request.user_id,
        current=current,
    )
    return MessageEnvelope(data={"message": "Ownership transferred successfully"})


@router.get(
    "/{organization_id}/members",
    response_model=MemberListEnvelope,
)
async def list_members(
    organization_id: UUID,
    current: OrganizationsRead,
    session: DatabaseSession,
) -> MemberListEnvelope:
    members = await service.list_members(
        session,
        organization_id=organization_id,
        current=current,
    )
    return MemberListEnvelope(
        data=[MemberResponse(**member) for member in members]
    )


@router.post(
    "/{organization_id}/members",
    response_model=InvitationEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    organization_id: UUID,
    request: InvitationCreate,
    current: UserLogin,
    session: DatabaseSession,
) -> InvitationEnvelope:
    invitation, invitation_status = await service.create_invitation(
        session,
        organization_id=organization_id,
        recipient_email=request.email,
        role=request.role,
        current=current,
    )
    return InvitationEnvelope(
        data=_invitation_response(invitation, invitation_status)
    )


@router.delete(
    "/{organization_id}/members/me",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def leave_organization(
    organization_id: UUID,
    current: UserLogin,
    session: DatabaseSession,
) -> Response:
    await service.leave_organization(
        session,
        organization_id=organization_id,
        current=current,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{organization_id}/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    organization_id: UUID,
    member_user_id: UUID,
    current: UserLogin,
    session: DatabaseSession,
) -> Response:
    await service.remove_member(
        session,
        organization_id=organization_id,
        member_user_id=member_user_id,
        current=current,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{organization_id}/invitations",
    response_model=InvitationEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    organization_id: UUID,
    request: InvitationCreate,
    current: UserLogin,
    session: DatabaseSession,
) -> InvitationEnvelope:
    invitation, invitation_status = await service.create_invitation(
        session,
        organization_id=organization_id,
        recipient_email=request.email,
        role=request.role,
        current=current,
    )
    return InvitationEnvelope(
        data=_invitation_response(invitation, invitation_status)
    )


@router.get(
    "/{organization_id}/invitations",
    response_model=InvitationListEnvelope,
)
async def list_invitations(
    organization_id: UUID,
    current: UserLogin,
    session: DatabaseSession,
) -> InvitationListEnvelope:
    invitations = await service.list_invitations(
        session,
        organization_id=organization_id,
        current=current,
    )
    return InvitationListEnvelope(
        data=[
            _invitation_response(inv, status) for inv, status in invitations
        ]
    )


@router.delete(
    "/{organization_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invitation(
    organization_id: UUID,
    invitation_id: UUID,
    current: UserLogin,
    session: DatabaseSession,
) -> Response:
    await service.revoke_invitation(
        session,
        organization_id=organization_id,
        invitation_id=invitation_id,
        current=current,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
