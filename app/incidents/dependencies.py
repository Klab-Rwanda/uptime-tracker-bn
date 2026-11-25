from functools import lru_cache

from app.services.notifications.delivery import DeliveryStore
from app.services.notifications.sql_store import SqlDeliveryStore
from app.util.database import engine


@lru_cache
def _default_alert_store() -> SqlDeliveryStore:
    return SqlDeliveryStore(engine)


def get_alert_store() -> DeliveryStore:
    """Where alerts are queued. Tests replace it with dependency_overrides."""
    return _default_alert_store()
