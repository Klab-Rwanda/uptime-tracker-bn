from functools import lru_cache

from app.services.email.base import EmailProvider
from app.services.email.brevo import BrevoEmailProvider
from app.util.config import get_settings


@lru_cache
def get_email_provider() -> EmailProvider:
    settings = get_settings()
    return BrevoEmailProvider(
        api_key=settings.brevo_api_key.get_secret_value(),
        sender_email=settings.brevo_sender_email,
        sender_name=settings.brevo_sender_name or "UpTimeTracker",
    )
