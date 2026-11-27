import logging

from app.auth.errors import APIError
from app.notification_channels.crypto import (
    ChannelCipher,
    ChannelCryptoError,
    get_cipher,
)
from app.services.email import get_email_provider
from app.services.email.base import EmailProvider
from app.services.slack import get_slack_sender
from app.services.slack.sender import SlackSender
from app.services.webhook import get_webhook_sender
from app.services.webhook.base import WebhookSender

logger = logging.getLogger(__name__)


def get_test_email_provider() -> EmailProvider | None:
    """The real email sender, or None if Brevo isn't configured (the test then
    reports that clearly). Tests replace it with dependency_overrides."""
    try:
        return get_email_provider()
    except ValueError:
        return None


def get_test_webhook_sender() -> WebhookSender:
    return get_webhook_sender()


def get_test_slack_sender() -> SlackSender:
    return get_slack_sender()


def get_channel_cipher() -> ChannelCipher:
    """FastAPI dependency. Tests replace it with dependency_overrides."""
    try:
        return get_cipher()
    except ChannelCryptoError as exc:
        logger.error("channel encryption is not configured correctly")
        raise APIError(
            503,
            "service_unavailable",
            "Notification channels are temporarily unavailable",
        ) from exc
