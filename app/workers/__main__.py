"""Start the background workers: `python -m app.workers`.

- scheduler:       runs due monitor checks, stores heartbeats, opens/resolves
                   incidents and queues their alerts
- delivery worker: sends queued alerts (email, webhook) and retries failures
"""

import asyncio
import logging

from app.util.logs import setup_logging
from app.workers.delivery_worker import run_delivery_worker
from app.workers.scheduler import run_scheduler

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    logger.info("Workers starting")
    # If one loop crashes the process exits, and the container restarts it
    await asyncio.gather(run_scheduler(), run_delivery_worker())


if __name__ == "__main__":
    asyncio.run(main())
