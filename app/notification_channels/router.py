from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.notification_channels import service
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import (
    get_channel_cipher,
    get_test_email_provider,
    get_test_webhook_sender,
)
from app.notification_channels.schemas import (
    ChannelCreate,
    ChannelEnvelope,
    ChannelListEnvelope,
    ChannelTestEnvelope,
    ChannelTestResponse,
    ChannelUpdate,
)
from app.services.email.base import EmailProvider
from app.services.webhook.base import WebhookSender
from app.util.database import get_db_session
from app.util.redis import get_redis_client

router = APIRouter(prefix="/v1/notification-channels", tags=["notification-channels"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]
Cipher = Annotated[ChannelCipher, Depends(get_channel_cipher)]
RedisClient = Annotated[Redis, Depends(get_redis_client)]
TestEmail = Annotated[EmailProvider | None, Depends(get_test_email_provider)]
TestWebhook = Annotated[WebhookSender, Depends(get_test_webhook_sender)]


@router.post("/{channel_id}/test", response_model=ChannelTestEnvelope)
async def test_channel(
    channel_id: UUID,
    current: CurrentAuth,
    session: DatabaseSession,
    cipher: Cipher,
    redis: RedisClient,
    email_provider: TestEmail,
    webhook_sender: TestWebhook,
) -> ChannelTestEnvelope:
    """Send one test message now. A delivery problem is reported in the result
    ("failed" plus a reason), not as an error status."""
    result = await service.test_channel(
        session,
        channel_id=channel_id,
        current=current,
        cipher=cipher,
        redis=redis,
        email_provider=email_provider,
        webhook_sender=webhook_sender,
    )
    return ChannelTestEnvelope(
        data=ChannelTestResponse(
            status=result.status,
            detail=result.detail,
            provider_message_id=result.provider_message_id,
            sent_at=result.sent_at,
        )
    )


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
