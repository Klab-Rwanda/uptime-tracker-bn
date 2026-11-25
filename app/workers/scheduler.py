import asyncio
import logging

from app.services.notifications.sql_store import SqlDeliveryStore
from app.util.database import engine, sessionLocal
from app.workers.check_worker import run_due_monitors

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


async def run_scheduler() -> None:
    logger.info("Scheduler started")
    alerts = SqlDeliveryStore(engine)  # incidents queue their alerts here

    while True:
        try:
            # A session factory, not one session: each monitor gets its own
            checked = await run_due_monitors(sessionLocal, alerts=alerts)
            if checked:
                logger.info("monitors checked", extra={"count": checked})
        except Exception:
            logger.exception("Scheduler encountered an error")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
