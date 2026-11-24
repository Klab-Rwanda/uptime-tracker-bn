import logging
from functools import lru_cache

from app.services.webhook.base import WebhookSender
from app.services.webhook.sender import HttpWebhookSender
from app.util.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_webhook_sender() -> WebhookSender:
    allow_private = get_settings().webhook_allow_private_targets
    if allow_private:
        logger.warning("Webhook private targets are ALLOWED. Never enable this in production.")
    return HttpWebhookSender(allow_private=allow_private)
