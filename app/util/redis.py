from functools import lru_cache

from redis.asyncio import Redis

from app.util.config import get_settings


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def check_redis() -> None:
    """Raises if Redis can't be reached."""
    await get_redis().ping()


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
