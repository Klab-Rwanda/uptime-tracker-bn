from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.notification_channels import service
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import get_channel_cipher
from app.notification_channels.schemas import (
    ChannelCreate,
    ChannelEnvelope,
    ChannelListEnvelope,
    ChannelUpdate,
)
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/notification-channels", tags=["notification-channels"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]
Cipher = Annotated[ChannelCipher, Depends(get_channel_cipher)]


@router.post(
    "",
    response_model=ChannelEnvelope,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_channel(
    data: ChannelCreate,
    response: Response,
    current: CurrentAuth,
    session: DatabaseSession,
    cipher: Cipher,
) -> ChannelEnvelope:
    channel, secret = await service.create_channel(
        session, data=data, current=current, cipher=cipher
    )
    response.headers["Cache-Control"] = "no-store"  # the response may hold a secret
    return ChannelEnvelope(
        data=service.to_channel_response(channel, cipher, signing_secret=secret)
    )


@router.get(
    "",
    response_model=ChannelListEnvelope,
    response_model_exclude_none=True,
)
async def list_channels(
    organization_id: Annotated[UUID, Query(description="Organization ID")],
    current: CurrentAuth,
    session: DatabaseSession,
    cipher: Cipher,
) -> ChannelListEnvelope:
    channels = await service.list_channels(
        session, organization_id=organization_id, current=current
    )
    return ChannelListEnvelope(
        data=[service.to_channel_response(channel, cipher) for channel in channels]
    )


@router.patch(
    "/{channel_id}",
    response_model=ChannelEnvelope,
    response_model_exclude_none=True,
)
async def update_channel(
    channel_id: UUID,
    data: ChannelUpdate,
    response: Response,
    current: CurrentAuth,
    session: DatabaseSession,
    cipher: Cipher,
) -> ChannelEnvelope:
    channel, secret = await service.update_channel(
        session, channel_id=channel_id, data=data, current=current, cipher=cipher
    )
    response.headers["Cache-Control"] = "no-store"
    return ChannelEnvelope(
        data=service.to_channel_response(channel, cipher, signing_secret=secret)
    )


@router.delete(
    "/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_channel(
    channel_id: UUID,
    current: CurrentAuth,
    session: DatabaseSession,
) -> None:
    await service.delete_channel(session, channel_id=channel_id, current=current)
