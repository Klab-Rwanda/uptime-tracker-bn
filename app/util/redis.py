from functools import lru_cache

from redis.asyncio import Redis

from app.util.config import get_settings


@lru_cache
def get_redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=False)


async def close_redis_client() -> None:
    if get_redis_client.cache_info().currsize:
        await get_redis_client().aclose()
        get_redis_client.cache_clear()