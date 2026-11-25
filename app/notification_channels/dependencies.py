import logging

from app.auth.errors import APIError
from app.notification_channels.crypto import (
    ChannelCipher,
    ChannelCryptoError,
    get_cipher,
)

logger = logging.getLogger(__name__)


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
