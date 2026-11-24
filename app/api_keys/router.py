from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys import service
from app.api_keys.schemas import (
    ApiKeyCreate,
    ApiKeyCreatedEnvelope,
    ApiKeyCreatedResponse,
    ApiKeyListEnvelope,
)
from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]


@router.post(
    "",
    response_model=ApiKeyCreatedEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    request: ApiKeyCreate,
    current: CurrentAuth,
    session: DatabaseSession,
) -> ApiKeyCreatedEnvelope:
    api_key, raw_token = await service.create_api_key(
        session,
        organization_id=request.organization_id,
        name=request.name,
        scopes=request.scopes,
        current=current,
    )
    return ApiKeyCreatedEnvelope(
        data=ApiKeyCreatedResponse(
            id=api_key.id,
            name=api_key.name,
            key=raw_token,
            scopes=service._deserialize_scopes(api_key.scopes),
            created_at=api_key.created_at,
        )
    )


@router.get("", response_model=ApiKeyListEnvelope)
async def list_api_keys(
    organization_id: Annotated[UUID, Query(description="Organization ID to filter API keys")],
    current: CurrentAuth,
    session: DatabaseSession,
) -> ApiKeyListEnvelope:
    keys = await service.list_api_keys(
        session,
        organization_id=organization_id,
        current=current,
    )
    return ApiKeyListEnvelope(
        data=[service.to_api_key_response(k) for k in keys]
    )


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    id: UUID,
    current: CurrentAuth,
    session: DatabaseSession,
) -> Response:
    await service.revoke_api_key(
        session,
        key_id=id,
        current=current,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
