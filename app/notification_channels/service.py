import json
import logging
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key_access import require_scope
from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.models.monitor import Monitor
from app.notification_channels.crypto import ChannelCipher, ChannelCryptoError
from app.notification_channels.models import NotificationChannel
from app.notification_channels.schemas import (
    ChannelCreate,
    ChannelResponse,
    ChannelUpdate,
    normalize_config,
    summarize_config,
)
from app.notification_channels.testing import (
    ChannelTestResult,
    enforce_test_rate_limit,
    send_test_notification,
)
from app.organizations.service import _get_org_membership
from app.services.email.base import EmailProvider
from app.services.webhook.base import WebhookSender
from app.services.webhook.signing import generate_secret
from app.util.config import get_settings

logger = logging.getLogger(__name__)

WRITE_ROLES = ("owner", "admin", "editor")
ADMIN_ROLES = ("owner", "admin")

# API keys need these scopes; user logins are limited by their role instead
READ_SCOPE = "notification_channels:read"
WRITE_SCOPE = "notification_channels:write"


def _name_conflict() -> APIError:
    return APIError(
        409,
        "conflict",
        "A notification channel with this name already exists in the organization",
    )


def _unreadable() -> APIError:
    return APIError(
        409,
        "channel_config_unreadable",
        "The stored configuration of this channel can no longer be read. "
        "Delete the channel and create it again.",
    )


async def _require_role(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    roles: tuple[str, ...],
    message: str,
) -> None:
    _, membership = await _get_org_membership(
        session, organization_id=organization_id, user_id=user_id
    )
    if membership.role not in roles:
        raise APIError(403, "forbidden", message)


async def _get_channel(session: AsyncSession, channel_id: UUID) -> NotificationChannel:
    channel = await session.get(NotificationChannel, channel_id)
    if channel is None:
        raise APIError(404, "channel_not_found", "Notification channel not found")
    return channel


async def _ensure_name_free(
    session: AsyncSession,
    *,
    organization_id: UUID,
    name: str,
    exclude_id: UUID | None = None,
) -> None:
    stmt = select(NotificationChannel.id).where(
        NotificationChannel.organization_id == organization_id,
        NotificationChannel.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(NotificationChannel.id != exclude_id)
    if await session.scalar(stmt) is not None:
        raise _name_conflict()


def _load_or_409(channel: NotificationChannel, cipher: ChannelCipher) -> dict[str, Any]:
    try:
        return cipher.decrypt(channel.config_encrypted)
    except ChannelCryptoError as exc:
        logger.warning(
            "channel configuration unreadable", extra={"channel_id": str(channel.id)}
        )
        raise _unreadable() from exc


def _text(stored: dict[str, Any] | None, key: str) -> str:
    value = stored.get(key) if stored else None
    if not isinstance(value, str) or not value:
        raise _unreadable()
    return value


def to_channel_response(
    channel: NotificationChannel,
    cipher: ChannelCipher,
    *,
    signing_secret: str | None = None,
) -> ChannelResponse:
    try:
        summary = summarize_config(
            channel.channel_type, cipher.decrypt(channel.config_encrypted)
        )
    except ChannelCryptoError:
        logger.warning(
            "channel configuration unreadable", extra={"channel_id": str(channel.id)}
        )
        summary = {"unreadable": True}
    return ChannelResponse(
        id=channel.id,
        organization_id=channel.organization_id,
        name=channel.name,
        type=channel.channel_type,
        config_summary=summary,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
        signing_secret=signing_secret,
    )


async def create_channel(
    session: AsyncSession,
    *,
    data: ChannelCreate,
    current: AuthenticatedUser,
    cipher: ChannelCipher,
) -> tuple[NotificationChannel, str | None]:
    require_scope(current, WRITE_SCOPE)
    await _require_role(
        session,
        organization_id=data.organization_id,
        user_id=current.user.id,
        roles=WRITE_ROLES,
        message="An editor role or higher is required to create a notification channel",
    )
    await _ensure_name_free(
        session, organization_id=data.organization_id, name=data.name
    )

    config = dict(data.config)
    signing_secret: str | None = None
    if data.type == "webhook":
        signing_secret = generate_secret()
        config["secret"] = signing_secret

    channel = NotificationChannel(
        organization_id=data.organization_id,
        name=data.name,
        channel_type=data.type,
        config_encrypted=cipher.encrypt(config),
        created_by_user_id=current.user.id,
    )
    session.add(channel)
    try:
        await session.commit()
    except IntegrityError as exc:  # two requests created the same name at once
        await session.rollback()
        raise _name_conflict() from exc
    await session.refresh(channel)
    return channel, signing_secret


async def list_channels(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> list[NotificationChannel]:
    require_scope(current, READ_SCOPE)
    await _get_org_membership(
        session, organization_id=organization_id, user_id=current.user.id
    )
    result = await session.execute(
        select(NotificationChannel)
        .where(NotificationChannel.organization_id == organization_id)
        .order_by(NotificationChannel.name, NotificationChannel.id)
    )
    return list(result.scalars().all())


async def update_channel(
    session: AsyncSession,
    *,
    channel_id: UUID,
    data: ChannelUpdate,
    current: AuthenticatedUser,
    cipher: ChannelCipher,
) -> tuple[NotificationChannel, str | None]:
    require_scope(current, WRITE_SCOPE)
    channel = await _get_channel(session, channel_id)
    await _require_role(
        session,
        organization_id=channel.organization_id,
        user_id=current.user.id,
        roles=WRITE_ROLES,
        message="An editor role or higher is required to update a notification channel",
    )
    if data.rotate_secret and channel.channel_type != "webhook":
        raise APIError(
            422, "validation_error", "Only webhook channels have a signing secret"
        )

    new_secret: str | None = None

    if data.name is not None and data.name != channel.name:
        await _ensure_name_free(
            session,
            organization_id=channel.organization_id,
            name=data.name,
            exclude_id=channel.id,
        )
        channel.name = data.name

    if data.config is not None or data.rotate_secret:
        new_config: dict[str, Any] | None = None
        if data.config is not None:
            try:
                new_config = normalize_config(
                    channel.channel_type,
                    data.config,
                    allow_private=get_settings().webhook_allow_private_targets,
                )
            except ValueError as exc:
                raise APIError(422, "validation_error", f"config: {exc}") from exc

        if channel.channel_type == "webhook":
            stored: dict[str, Any] | None = None
            if new_config is None or not data.rotate_secret:
                stored = _load_or_409(channel, cipher)
            config = (
                dict(new_config)
                if new_config is not None
                else {"webhook_url": _text(stored, "webhook_url")}
            )
            if data.rotate_secret:
                new_secret = generate_secret()
                config["secret"] = new_secret
            else:
                config["secret"] = _text(stored, "secret")
            channel.config_encrypted = cipher.encrypt(config)
        else:
            if new_config is None:
                raise APIError(422, "validation_error", "config is required")
            channel.config_encrypted = cipher.encrypt(new_config)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _name_conflict() from exc
    await session.refresh(channel)
    return channel, new_secret


async def test_channel(
    session: AsyncSession,
    *,
    channel_id: UUID,
    current: AuthenticatedUser,
    cipher: ChannelCipher,
    redis: Redis,
    email_provider: EmailProvider | None,
    webhook_sender: WebhookSender | None,
) -> ChannelTestResult:
    """Send one test message through the channel (editor or higher).

    Access is checked before the rate limit, so someone outside the
    organization can't use up a channel's tests.
    """
    require_scope(current, WRITE_SCOPE)
    channel = await _get_channel(session, channel_id)
    await _require_role(
        session,
        organization_id=channel.organization_id,
        user_id=current.user.id,
        roles=WRITE_ROLES,
        message="An editor role or higher is required to test a notification channel",
    )
    config = _load_or_409(channel, cipher)
    await enforce_test_rate_limit(redis, channel.id)
    return await send_test_notification(
        channel, config, email_provider=email_provider, webhook_sender=webhook_sender
    )


def _without_channel(raw: str | None, channel_id: str) -> str | None:
    """A monitor's stored channel list with one channel removed (None if unchanged)."""
    try:
        ids = json.loads(raw or "[]")
    except ValueError:
        return None
    if not isinstance(ids, list) or channel_id not in ids:
        return None
    return json.dumps([value for value in ids if value != channel_id])


async def delete_channel(
    session: AsyncSession,
    *,
    channel_id: UUID,
    current: AuthenticatedUser,
) -> None:
    require_scope(current, WRITE_SCOPE)
    channel = await _get_channel(session, channel_id)
    await _require_role(
        session,
        organization_id=channel.organization_id,
        user_id=current.user.id,
        roles=ADMIN_ROLES,
        message="An owner or administrator role is required to delete a notification channel",
    )
    # monitors.notification_channel_ids is a JSON list without a foreign key, so
    # remove the deleted channel from this organization's monitors ourselves
    monitors = await session.scalars(
        select(Monitor).where(
            Monitor.organization_id == channel.organization_id,
            Monitor.notification_channel_ids.is_not(None),
        )
    )
    for monitor in monitors:
        updated = _without_channel(monitor.notification_channel_ids, str(channel.id))
        if updated is not None:
            monitor.notification_channel_ids = updated
    await session.delete(channel)
    await session.commit()


async def validate_channel_ids(
    session: AsyncSession,
    *,
    organization_id: UUID,
    channel_ids: list[str],
) -> list[str]:
    """For monitor create/update: every ID must be a channel of THIS organization.

    Returns the IDs in canonical form, without duplicates.
    """
    unique: list[UUID] = []
    for raw in channel_ids:
        try:
            parsed = UUID(raw)
        except ValueError:
            raise APIError(
                422,
                "validation_error",
                "notification_channel_ids must contain valid IDs",
            ) from None
        if parsed not in unique:
            unique.append(parsed)
    if not unique:
        return []

    found = set(
        (
            await session.scalars(
                select(NotificationChannel.id).where(
                    NotificationChannel.organization_id == organization_id,
                    NotificationChannel.id.in_(unique),
                )
            )
        ).all()
    )
    if len(found) != len(unique):
        raise APIError(
            404,
            "channel_not_found",
            "One or more notification channels do not exist in this organization",
        )
    return [str(channel_id) for channel_id in unique]
