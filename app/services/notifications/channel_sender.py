"""Sends one queued alert (a DeliveryRecord) through its notification channel.

The dispatcher calls ChannelDeliverySender for every due delivery. Everything is
checked again at send time, because time has passed since the alert was queued:
the channel must still exist, belong to the incident's organization, and have a
readable configuration. Those failures are permanent (never retried).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.notification_channels.crypto import ChannelCipher, ChannelCryptoError
from app.notification_channels.models import NotificationChannel
from app.services.email.base import EmailMessage, EmailProvider
from app.services.email.templates import (
    IncidentEmailContext,
    render_incident_opened,
    render_incident_resolved,
)
from app.services.notifications.delivery import DeliveryRecord
from app.services.notifications.retry import PermanentDeliveryError
from app.services.slack.messages import incident_message
from app.services.slack.sender import SlackSender
from app.services.webhook.base import WebhookEvent, WebhookSender

# The incident fields a webhook receiver gets (never secrets or internal state)
WEBHOOK_FIELDS = (
    "incident_id",
    "monitor_id",
    "monitor_name",
    "target",
    "title",
    "cause",
    "status",
    "started_at",
    "resolved_at",
)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)  # understands the trailing "Z" since 3.11
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ChannelDeliverySender:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cipher: ChannelCipher,
        email_provider: EmailProvider | None = None,
        webhook_sender: WebhookSender | None = None,
        slack_sender: SlackSender | None = None,
    ) -> None:
        self._sessions = session_factory
        self._cipher = cipher
        self._email = email_provider
        self._webhook = webhook_sender
        self._slack = slack_sender

    async def __call__(self, record: DeliveryRecord) -> str | None:
        payload = record.payload
        channel = await self._load_channel(record.channel_id)
        if str(channel.organization_id) != str(payload.get("organization_id")):
            # Never let an alert leave the incident's organization
            raise PermanentDeliveryError("Notification channel belongs to another organization")
        try:
            config = self._cipher.decrypt(channel.config_encrypted)
        except ChannelCryptoError as exc:
            raise PermanentDeliveryError("Notification channel configuration can't be read") from exc

        if channel.channel_type == "email":
            return await self._send_email(record, config)
        if channel.channel_type == "webhook":
            return await self._send_webhook(record, config)
        if channel.channel_type == "slack":
            return await self._send_slack(record, config)
        raise PermanentDeliveryError("Unsupported notification channel type")

    async def _load_channel(self, channel_id: str) -> NotificationChannel:
        try:
            uid = UUID(channel_id)
        except ValueError as exc:
            raise PermanentDeliveryError("Notification channel not found") from exc
        async with self._sessions() as session:
            channel = await session.get(NotificationChannel, uid)
        if channel is None:  # deleted after the alert was queued
            raise PermanentDeliveryError("Notification channel not found")
        return channel

    async def _send_email(self, record: DeliveryRecord, config: dict[str, Any]) -> str | None:
        if self._email is None:
            raise PermanentDeliveryError("Email delivery is not configured")
        recipients = config.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            raise PermanentDeliveryError("Email channel has no recipients")

        payload = record.payload
        started_at = _parse_time(payload.get("started_at"))
        if started_at is None:
            raise PermanentDeliveryError("Alert payload is incomplete")
        context = IncidentEmailContext(
            monitor_name=str(payload.get("monitor_name", "")),
            target=str(payload.get("target", "")),
            incident_id=str(payload.get("incident_id", "")),
            started_at=started_at,
            cause=payload.get("cause"),
            resolved_at=_parse_time(payload.get("resolved_at")),
        )
        mail = (
            render_incident_resolved(context)
            if payload.get("event") == "resolved"
            else render_incident_opened(context)
        )
        result = await self._email.send(
            EmailMessage(
                to=[str(address) for address in recipients],
                subject=mail.subject,
                html=mail.html,
                text=mail.text,
                idempotency_key=record.idempotency_key,
            )
        )
        return result.message_id

    async def _send_webhook(self, record: DeliveryRecord, config: dict[str, Any]) -> str | None:
        if self._webhook is None:
            raise PermanentDeliveryError("Webhook delivery is not configured")
        url, secret = config.get("webhook_url"), config.get("secret")
        if not isinstance(url, str) or not isinstance(secret, str) or not url or not secret:
            raise PermanentDeliveryError("Notification channel configuration can't be read")

        payload = record.payload
        event = WebhookEvent(
            event_type=f"incident.{payload.get('event')}",
            # Receivers drop repeats with this (X-UpTime-Delivery header)
            delivery_id=record.idempotency_key,
            created_at=record.created_at,
            data={field: payload.get(field) for field in WEBHOOK_FIELDS},
        )
        result = await self._webhook.send(url, secret, event)
        return f"http-{result.status_code}"

    async def _send_slack(self, record: DeliveryRecord, config: dict[str, Any]) -> str | None:
        if self._slack is None:
            raise PermanentDeliveryError("Slack delivery is not configured")
        url = config.get("webhook_url")
        if not isinstance(url, str) or not url:
            raise PermanentDeliveryError("Notification channel configuration can't be read")
        status = await self._slack.send(url, incident_message(record.payload))
        return f"slack-{status}"
