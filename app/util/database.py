from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.util.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url,
        pool_pre_ping=True,   # drop dead connections (Neon suspends idle computes)
        pool_recycle=300,
        # Needed if you ever use Neon's pooled (-pooler) endpoint with asyncpg
        connect_args={"statement_cache_size": 0},
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one database session per request."""
    async with get_sessionmaker()() as session:
        yield session


async def check_database() -> None:
    """Raises if the database can't be reached."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
