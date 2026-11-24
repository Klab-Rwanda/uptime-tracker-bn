import asyncio
import logging

from app.util.database import sessionLocal
from app.workers.check_worker import run_due_monitors

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


async def run_scheduler() -> None:
    logger.info("Scheduler started")

    while True:
        try:
            async with sessionLocal() as db:
                await run_due_monitors(db)
        except Exception:
            logger.exception("Scheduler encountered an error")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
