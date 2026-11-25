from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.incident import Incident, IncidentEvent
from app.models.monitor import Monitor
from app.organizations.models import Organization
from tests.conftest import dispose_test_engine, make_test_engine

NOW = datetime.now(UTC)


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def session(request, tmp_path) -> AsyncIterator[AsyncSession]:
    engine = await make_test_engine(request.param, tmp_path)  # every table, FKs enforced
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        db.add(org)
        await db.commit()
        db.info["org"] = org.id
        yield db
    await dispose_test_engine(engine)


async def new_monitor(session: AsyncSession) -> UUID:
    monitor = Monitor(
        organization_id=session.info["org"],
        name=f"Monitor {uuid4().hex[:4]}",
        monitor_type="http",
        target="https://api.example.com/health",
        next_check_at=NOW,
    )
    session.add(monitor)
    await session.commit()
    return monitor.id


async def make_incident(session: AsyncSession, monitor_id: UUID | None = None, **overrides) -> Incident:
    values = {
        "organization_id": session.info["org"],
        "monitor_id": monitor_id or await new_monitor(session),
        "title": "Customer API is down",
        "cause": "HTTP 503 from API",
        "started_at": NOW,
    }
    values.update(overrides)
    return Incident(**values)


async def test_create_incident_with_timeline(session):
    incident = await make_incident(session)
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
    monitor_id = await new_monitor(session)
    session.add(await make_incident(session, monitor_id))
    await session.commit()

    session.add(await make_incident(session, monitor_id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_new_incident_allowed_after_previous_resolved(session):
    monitor_id = await new_monitor(session)
    session.add(
        await make_incident(
            session, monitor_id, status="resolved", resolved_at=NOW + timedelta(minutes=5)
        )
    )
    session.add(await make_incident(session, monitor_id))
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
    session.add(await make_incident(session, **overrides))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.parametrize("column", ["monitor_id", "organization_id"])
async def test_unknown_monitor_or_organization_is_rejected(session, column):
    session.add(await make_incident(session, **{column: uuid4()}))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_note_event_requires_message(session):
    incident = await make_incident(session)
    incident.events.append(IncidentEvent(event_type="note"))
    session.add(incident)
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_unknown_event_type_rejected(session):
    incident = await make_incident(session)
    incident.events.append(IncidentEvent(event_type="exploded"))
    session.add(incident)
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_deleting_incident_deletes_its_timeline(session):
    incident = await make_incident(session)
    incident.events.append(IncidentEvent(event_type="opened"))
    session.add(incident)
    await session.commit()

    # Raw SQL, so this checks the database's ON DELETE CASCADE, not the ORM
    await session.execute(text("DELETE FROM incidents"))
    await session.commit()
    remaining = (await session.execute(select(IncidentEvent))).scalars().all()
    assert remaining == []
