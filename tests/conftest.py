import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.main import app
from app.models.notification_delivery import NotificationDelivery
from app.services.notifications.delivery import DeliveryStore, InMemoryDeliveryStore
from app.services.notifications.sql_store import SqlDeliveryStore


@pytest.fixture
async def client():
    """Calls the FastAPI app directly, with no network."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    """TODO: replace with a real login once Developer A's auth exists."""
    return {"Authorization": "Bearer TODO"}


@pytest.fixture
def outsider_headers():
    """TODO: a user from a DIFFERENT organization, for isolation tests."""
    return {"Authorization": "Bearer TODO-OUTSIDER"}


# --- delivery stores ---------------------------------------------------------
# Every delivery-store test runs against each store:
#   memory:   InMemoryDeliveryStore
#   sqlite:   SqlDeliveryStore on a file database (separate connections, real locking)
#   postgres: SqlDeliveryStore on PostgreSQL, only when TEST_POSTGRES_URL is set

_DELIVERY_TABLE = NotificationDelivery.__table__


async def _make_delivery_engine(backend: str, tmp_path: Path) -> AsyncEngine:
    if backend == "sqlite":
        url = f"sqlite+aiosqlite:///{(tmp_path / 'deliveries.db').as_posix()}"
        engine = create_async_engine(url, connect_args={"timeout": 30})
    else:
        url = os.environ.get("TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("set TEST_POSTGRES_URL to run the PostgreSQL store tests")
        engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: _DELIVERY_TABLE.drop(sync, checkfirst=True))
        await conn.run_sync(lambda sync: _DELIVERY_TABLE.create(sync))
    return engine


async def _drop_delivery_engine(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: _DELIVERY_TABLE.drop(sync, checkfirst=True))
    await engine.dispose()


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def delivery_engine(request, tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = await _make_delivery_engine(request.param, tmp_path)
    yield engine
    await _drop_delivery_engine(engine)


@pytest_asyncio.fixture(params=["memory", "sqlite", "postgres"])
async def store(request, tmp_path) -> AsyncIterator[DeliveryStore]:
    if request.param == "memory":
        yield InMemoryDeliveryStore()
        return
    engine = await _make_delivery_engine(request.param, tmp_path)
    yield SqlDeliveryStore(engine)
    await _drop_delivery_engine(engine)
