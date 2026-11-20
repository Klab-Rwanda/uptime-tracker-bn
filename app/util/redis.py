from functools import lru_cache

from redis.asyncio import Redis

from app.util.config import settings


@lru_cache
def get_redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=False)


def get_redis() -> Redis:
    return get_redis_client()


async def check_redis() -> None:
    await get_redis_client().ping()


async def close_redis_client() -> None:
    if get_redis_client.cache_info().currsize:
        await get_redis_client().aclose()
        get_redis_client.cache_clear()


async def close_redis() -> None:
    await close_redis_client()
