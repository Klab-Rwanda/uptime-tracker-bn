"""The "Test this channel" button: send one real message now and report what happened.

Customers use it right after setting up a channel, so they learn about a typo in
an address or a broken webhook before a real outage, not during one.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.auth.errors import APIError
from app.notification_channels.models import NotificationChannel
from app.services.email.base import EmailMessage, EmailProvider
from app.services.email.templates import render_channel_test
from app.services.notifications.retry import PermanentDeliveryError, classify_failure
from app.services.slack.messages import channel_test_message
from app.services.slack.sender import SlackSender
from app.services.webhook.base import WebhookEvent, WebhookSender

logger = logging.getLogger(__name__)

TEST_TIMEOUT_SECONDS = 15.0
# Each test reaches a real inbox or server: limit it so the button can't be used
# to spam someone through our servers
MAX_TESTS_PER_WINDOW = 5
WINDOW_SECONDS = 600

_INCREMENT_WITH_EXPIRY = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


@dataclass(frozen=True)
class ChannelTestResult:
    status: Literal["sent", "failed"]
    detail: str | None  # why it failed; safe to show (no URLs, no secrets)
    provider_message_id: str | None
    sent_at: datetime


async def enforce_test_rate_limit(redis: Redis, channel_id: object) -> None:
    try:
        count = await redis.eval(
            _INCREMENT_WITH_EXPIRY, 1, f"channels:test:{channel_id}", WINDOW_SECONDS
        )
    except RedisError as exc:
        raise APIError(
            503, "service_unavailable", "Channel tests are temporarily unavailable"
        ) from exc
    if int(count) > MAX_TESTS_PER_WINDOW:
        raise APIError(
            429,
            "rate_limit_exceeded",
            f"At most {MAX_TESTS_PER_WINDOW} tests per channel every "
            f"{WINDOW_SECONDS // 60} minutes. Try again later.",
            headers={"Retry-After": str(WINDOW_SECONDS)},
        )


async def send_test_notification(
    channel: NotificationChannel,
    config: dict[str, Any],
    *,
    email_provider: EmailProvider | None,
    webhook_sender: WebhookSender | None,
    slack_sender: SlackSender | None = None,
    now: datetime | None = None,
) -> ChannelTestResult:
    """Send one test message (one attempt, no retries) and report the outcome."""
    sent_at = now or datetime.now(UTC)
    try:
        message_id = await asyncio.wait_for(
            _send(channel, config, email_provider, webhook_sender, slack_sender, sent_at),
            timeout=TEST_TIMEOUT_SECONDS,
        )
    # The outcome is the answer, whatever went wrong; classify_failure keeps
    # only messages that are safe to show (never URLs or tracebacks)
    except Exception as exc:  # noqa: BLE001
        failure = classify_failure(exc)
        logger.info(
            "channel test failed",
            extra={"channel_id": str(channel.id), "error": failure.message},
        )
        return ChannelTestResult("failed", failure.message, None, sent_at)
    logger.info("channel test sent", extra={"channel_id": str(channel.id)})
    return ChannelTestResult("sent", None, message_id, sent_at)


async def _send(
    channel: NotificationChannel,
    config: dict[str, Any],
    email_provider: EmailProvider | None,
    webhook_sender: WebhookSender | None,
    slack_sender: SlackSender | None,
    sent_at: datetime,
) -> str | None:
    key = f"test:{channel.id}:{uuid4().hex}"  # every test is a new message

    if channel.channel_type == "email":
        if email_provider is None:
            raise PermanentDeliveryError("Email delivery is not configured")
        recipients = config.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            raise PermanentDeliveryError("Email channel has no recipients")
        mail = render_channel_test(channel.name, sent_at)
        result = await email_provider.send(
            EmailMessage(
                to=[str(address) for address in recipients],
                subject=mail.subject,
                html=mail.html,
                text=mail.text,
                idempotency_key=key,
            )
        )
        return result.message_id

    if channel.channel_type == "webhook":
        if webhook_sender is None:
            raise PermanentDeliveryError("Webhook delivery is not configured")
        url, secret = config.get("webhook_url"), config.get("secret")
        if not isinstance(url, str) or not isinstance(secret, str) or not url or not secret:
            raise PermanentDeliveryError("Notification channel configuration can't be read")
        response = await webhook_sender.send(
            url,
            secret,
            WebhookEvent(
                event_type="channel.test",
                delivery_id=key,
                created_at=sent_at,
                data={
                    "channel_id": str(channel.id),
                    "channel_name": channel.name,
                    "message": "This is a test notification from UpTimeTracker",
                },
            ),
        )
        return f"http-{response.status_code}"

    if channel.channel_type == "slack":
        if slack_sender is None:
            raise PermanentDeliveryError("Slack delivery is not configured")
        slack_url = config.get("webhook_url")
        if not isinstance(slack_url, str) or not slack_url:
            raise PermanentDeliveryError("Notification channel configuration can't be read")
        status = await slack_sender.send(slack_url, channel_test_message(channel.name, sent_at))
        return f"slack-{status}"

    raise PermanentDeliveryError("Unsupported notification channel type")
