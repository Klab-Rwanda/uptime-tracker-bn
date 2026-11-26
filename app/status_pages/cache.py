"""Redis cache for public status pages.

During an outage many visitors open the same status page at once. The finished
page is kept in Redis for a few seconds, so the database is asked at most once
per page per CACHE_SECONDS. Unknown slugs are remembered too, so guessing random
addresses cannot flood the database either.

The cache is only a speed-up: if Redis is down or slow, the page is built from
the database as if nothing was cached.
"""

import asyncio
import logging
from typing import Final

from prometheus_client import Counter
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.status_pages.schemas import PublicStatusPage

logger = logging.getLogger(__name__)

CACHE_SECONDS = 15
# A slow Redis must not make the status page slow
REDIS_TIMEOUT_SECONDS = 0.5
KEY_PREFIX = "status_pages:public:"
_MISSING_VALUE = b"-"

CACHE_LOOKUPS = Counter(
    "status_page_cache_lookups_total",
    "Public status page cache lookups",
    ["result"],  # hit, missing (cached 404), miss, error
)


class _Missing:
    """Marker: the cache knows this slug has no page."""


MISSING: Final = _Missing()


def cache_key(slug: str) -> str:
    return f"{KEY_PREFIX}{slug}"


async def read(redis: Redis, slug: str) -> PublicStatusPage | _Missing | None:
    """The cached page, MISSING for a cached "not found", or None to build it."""
    try:
        async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
            raw = await redis.get(cache_key(slug))
    except (RedisError, TimeoutError, OSError):
        logger.warning("status page cache unavailable, reading from the database")
        CACHE_LOOKUPS.labels(result="error").inc()
        return None
    if raw is None:
        CACHE_LOOKUPS.labels(result="miss").inc()
        return None
    if raw == _MISSING_VALUE:
        CACHE_LOOKUPS.labels(result="missing").inc()
        return MISSING
    try:
        page = PublicStatusPage.model_validate_json(raw)
    except ValidationError:
        # Written by an older version of the code: just build it again
        CACHE_LOOKUPS.labels(result="miss").inc()
        return None
    CACHE_LOOKUPS.labels(result="hit").inc()
    return page


async def _set(redis: Redis, slug: str, value: bytes) -> None:
    try:
        async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
            await redis.set(cache_key(slug), value, ex=CACHE_SECONDS)
    except (RedisError, TimeoutError, OSError):
        logger.warning("could not store a status page in the cache")


async def store(redis: Redis, slug: str, page: PublicStatusPage) -> None:
    await _set(redis, slug, page.model_dump_json().encode())


async def store_missing(redis: Redis, slug: str) -> None:
    await _set(redis, slug, _MISSING_VALUE)


async def forget(redis: Redis, *slugs: str) -> None:
    """Drop cached copies after a page changes. If this fails, visitors see the
    old version for at most CACHE_SECONDS."""
    keys = [cache_key(slug) for slug in dict.fromkeys(slugs)]
    if not keys:
        return
    try:
        async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
            await redis.delete(*keys)
    except (RedisError, TimeoutError, OSError):
        logger.warning("could not clear cached status pages")
