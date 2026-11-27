import asyncio
import logging

from app.notification_channels.crypto import get_cipher
from app.services.email import get_email_provider
from app.services.email.base import EmailProvider
from app.services.notifications.channel_sender import ChannelDeliverySender
from app.services.notifications.dispatcher import process_due
from app.services.notifications.sql_store import SqlDeliveryStore
from app.services.slack import get_slack_sender
from app.services.webhook import get_webhook_sender
from app.util.database import engine, sessionLocal

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


def build_sender() -> ChannelDeliverySender:
    """The production sender. Fails at startup if channel encryption is broken,
    because then no alert could ever be sent."""
    email: EmailProvider | None
    try:
        email = get_email_provider()
    except ValueError:
        # Email alerts fail permanently until BREVO_API_KEY/BREVO_SENDER_EMAIL are set
        logger.error("email alerts are disabled: Brevo is not configured")
        email = None
    return ChannelDeliverySender(
        sessionLocal,
        cipher=get_cipher(),
        email_provider=email,
        webhook_sender=get_webhook_sender(),
        slack_sender=get_slack_sender(),
    )


async def run_delivery_worker(*, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    """Send due alerts forever: new ones, and retries whose time has come."""
    store = SqlDeliveryStore(engine)
    sender = build_sender()
    logger.info("Delivery worker started")

    while True:
        try:
            counts = await process_due(store, sender)
            if counts:
                logger.info("alerts processed", extra={k.value: v for k, v in counts.items()})
        except Exception:
            # A database blip must not stop the worker; the next round retries
            logger.exception("Delivery worker encountered an error")
        await asyncio.sleep(poll_interval)
