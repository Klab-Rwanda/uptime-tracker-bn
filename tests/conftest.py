import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Table, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import app.models.incident
from app.main import app
from app.models.notification_delivery import NotificationDelivery
from app.services.notifications.delivery import DeliveryStore, InMemoryDeliveryStore
from app.services.notifications.sql_store import SqlDeliveryStore
from app.util.database import Base


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

_TEST_SCHEMAS: dict[int, str] = {}  # engine -> its throwaway PostgreSQL schema


async def make_test_engine(
    backend: str, tmp_path: Path, tables: list[Table] | None = None
) -> AsyncEngine:
    """A database for one test: a SQLite file, or a throwaway PostgreSQL schema.

    PostgreSQL tests run in their own schema (dropped by dispose_test_engine), so
    they never touch tables created by migrations in the same database.
    `tables=None` creates every model's table.
    """
    if backend == "sqlite":
        url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
        engine = create_async_engine(url, connect_args={"timeout": 30})

        # SQLite ignores foreign keys unless asked; enforce them like PostgreSQL does
        @event.listens_for(engine.sync_engine, "connect")
        def _foreign_keys_on(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    else:
        url = os.environ.get("TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("set TEST_POSTGRES_URL to run the PostgreSQL tests")
        schema = f"test_{uuid4().hex[:12]}"
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}}
        )
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        _TEST_SCHEMAS[id(engine)] = schema
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    return engine


async def dispose_test_engine(engine: AsyncEngine) -> None:
    schema = _TEST_SCHEMAS.pop(id(engine), None)
    if schema is not None:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()


_DELIVERY_TABLES = [cast(Table, NotificationDelivery.__table__)]


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def delivery_engine(request, tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = await make_test_engine(request.param, tmp_path, _DELIVERY_TABLES)
    yield engine
    await dispose_test_engine(engine)


@pytest_asyncio.fixture(params=["memory", "sqlite", "postgres"])
async def store(request, tmp_path) -> AsyncIterator[DeliveryStore]:
    if request.param == "memory":
        yield InMemoryDeliveryStore()
        return
    engine = await make_test_engine(request.param, tmp_path, _DELIVERY_TABLES)
    yield SqlDeliveryStore(engine)
    await dispose_test_engine(engine)
