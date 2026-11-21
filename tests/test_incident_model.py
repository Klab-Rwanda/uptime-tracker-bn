from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.models import User
from app.models.incident import Incident, IncidentEvent
from app.util.database import Base

NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite ignores foreign keys unless asked, and we test ON DELETE CASCADE
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    tables = [User.__table__, Incident.__table__, IncidentEvent.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tables)

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


def make_incident(monitor_id=None, **overrides) -> Incident:
    values = {
        "organization_id": uuid4(),
        "monitor_id": monitor_id or uuid4(),
        "title": "Customer API is down",
        "cause": "HTTP 503 from API",
        "started_at": NOW,
    }
    values.update(overrides)
    return Incident(**values)


async def test_create_incident_with_timeline(session):
    incident = make_incident()
    incident.events.append(IncidentEvent(event_type="opened"))
    incident.events.append(IncidentEvent(event_type="note", message="Looking into it"))
    session.add(incident)
    await session.commit()

    saved = (await session.execute(select(Incident))).scalar_one()
    assert saved.status == "open"
    assert saved.resolved_at is None
    events = (await session.execute(select(IncidentEvent))).scalars().all()
    assert {e.event_type for e in events} == {"opened", "note"}


async def test_only_one_active_incident_per_monitor(session):
    monitor_id = uuid4()
    session.add(make_incident(monitor_id))
    await session.commit()

    session.add(make_incident(monitor_id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_new_incident_allowed_after_previous_resolved(session):
    monitor_id = uuid4()
    session.add(
        make_incident(
            monitor_id, status="resolved", resolved_at=NOW + timedelta(minutes=5)
        )
    )
    session.add(make_incident(monitor_id))
    await session.commit()

    count = len((await session.execute(select(Incident))).scalars().all())
    assert count == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "exploded"},
        {"status": "resolved"},  # resolved without resolved_at
        {"resolved_at": NOW},  # resolved_at on an open incident
        {"status": "resolved", "resolved_at": NOW - timedelta(minutes=1)},
        {"acknowledged_at": NOW - timedelta(minutes=1)},
    ],
)
async def test_invalid_incident_rejected(session, overrides):
    session.add(make_incident(**overrides))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_note_event_requires_message(session):
    incident = make_incident()
    incident.events.append(IncidentEvent(event_type="note"))
    session.add(incident)
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_unknown_event_type_rejected(session):
    incident = make_incident()
    incident.events.append(IncidentEvent(event_type="exploded"))
    session.add(incident)
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_deleting_incident_deletes_its_timeline(session):
    incident = make_incident()
    incident.events.append(IncidentEvent(event_type="opened"))
    session.add(incident)
    await session.commit()

    # Raw SQL, so this checks the database's ON DELETE CASCADE, not the ORM
    await session.execute(text("DELETE FROM incidents"))
    await session.commit()
    remaining = (await session.execute(select(IncidentEvent))).scalars().all()
    assert remaining == []
