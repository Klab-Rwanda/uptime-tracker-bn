"""Opening, resolving and annotating incidents.

These functions trust their caller on WHO may act: the API layer checks
membership and roles first. They do guarantee the data rules:

- an incident always belongs to its monitor's organization;
- at most one unresolved incident per monitor (also enforced by the database);
- status changes follow app/services/incident_logic.py and are atomic, so two
  people (or a person and the detector) can't both resolve the same incident;
- every change is written to the incident's timeline.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.incidents.history import DEFAULT_HISTORY_LIMIT, CheckHistory
from app.models.incident import Incident, IncidentEvent
from app.models.monitor import Monitor
from app.services.incident_logic import (
    IncidentEventType,
    IncidentStatus,
    InvalidIncidentTransition,
    transition,
)
from app.services.incident_rules import IncidentAction, IncidentRule, decide

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255
MAX_NOTE_LENGTH = 5000


class IncidentNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Evaluation:
    """What evaluate_monitor() did."""

    action: IncidentAction  # OPEN or RESOLVE only when this call made the change
    reason: str
    incident: Incident | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    # SQLite returns timestamps without a timezone; everything we store is UTC
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


async def get_active_incident(session: AsyncSession, monitor_id: UUID) -> Incident | None:
    """The monitor's unresolved (open or acknowledged) incident, if any."""
    return await session.scalar(
        select(Incident).where(
            Incident.monitor_id == monitor_id,
            Incident.status != IncidentStatus.RESOLVED.value,
        )
    )


async def get_incident(session: AsyncSession, incident_id: UUID) -> Incident:
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise IncidentNotFound("Incident not found")
    return incident


async def get_timeline(session: AsyncSession, incident_id: UUID) -> list[IncidentEvent]:
    result = await session.scalars(
        select(IncidentEvent)
        .where(IncidentEvent.incident_id == incident_id)
        .order_by(IncidentEvent.created_at, IncidentEvent.id)
    )
    return list(result.all())


async def open_incident(
    session: AsyncSession,
    monitor: Monitor,
    *,
    started_at: datetime,
    cause: str | None = None,
    now: datetime | None = None,
) -> tuple[Incident, bool]:
    """Open an incident for the monitor. Returns (incident, created).

    If the monitor already has an unresolved incident (another worker was
    faster), that one is returned with created=False instead of a duplicate.
    """
    _require_aware(started_at, "started_at")
    now = now or _utcnow()
    # Read these now: a rollback below expires `monitor`, and reloading it lazily
    # isn't possible in async code
    monitor_id, organization_id = monitor.id, monitor.organization_id
    incident = Incident(
        organization_id=organization_id,  # always the monitor's own tenant
        monitor_id=monitor_id,
        status=IncidentStatus.OPEN.value,
        title=f"{monitor.name} is down"[:MAX_TITLE_LENGTH],
        cause=cause,
        started_at=started_at,
    )
    incident.events.append(
        IncidentEvent(event_type=IncidentEventType.OPENED.value, message=cause, created_at=now)
    )
    session.add(incident)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await get_active_incident(session, monitor_id)
        if existing is None:  # not the one-active-incident rule: a real error
            raise
        return existing, False
    logger.info(
        "incident opened",
        extra={"incident_id": str(incident.id), "monitor_id": str(monitor_id)},
    )
    return incident, True


async def _change_status(
    session: AsyncSession,
    incident_id: UUID,
    *,
    target: IncidentStatus,
    event: IncidentEvent,
    values_for: Any,
) -> Incident:
    """Move an incident to `target` atomically and record `event`.

    `values_for(incident)` returns the extra columns to set. The UPDATE only
    applies if the status is still the one we validated, so a concurrent change
    makes this fail instead of silently overwriting it.
    """
    incident = await get_incident(session, incident_id)
    current = incident.status
    transition(current, target.value)  # raises InvalidIncidentTransition

    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Incident)
            .where(Incident.id == incident_id, Incident.status == current)
            .values(status=target.value, **values_for(incident))
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        await session.rollback()
        latest = await get_incident(session, incident_id)
        await session.refresh(latest)
        raise InvalidIncidentTransition(
            f"Cannot change incident from {latest.status} to {target.value}"
        )
    event.incident_id = incident_id
    session.add(event)
    await session.commit()
    await session.refresh(incident)
    return incident


async def resolve_incident(
    session: AsyncSession,
    incident_id: UUID,
    *,
    resolved_at: datetime | None = None,
    actor_user_id: UUID | None = None,
    message: str | None = None,
    now: datetime | None = None,
) -> Incident:
    """Resolve an open or acknowledged incident.

    `resolved_at` is when the service recovered (Decision.since) and defaults
    to now. It is never set before the incident started.
    """
    now = now or _utcnow()
    when = _require_aware(resolved_at or now, "resolved_at")
    incident = await _change_status(
        session,
        incident_id,
        target=IncidentStatus.RESOLVED,
        event=IncidentEvent(
            event_type=IncidentEventType.RESOLVED.value,
            message=message,
            actor_user_id=actor_user_id,
            created_at=now,
        ),
        values_for=lambda i: {"resolved_at": max(when, _aware(i.started_at))},
    )
    logger.info("incident resolved", extra={"incident_id": str(incident_id)})
    return incident


async def acknowledge_incident(
    session: AsyncSession,
    incident_id: UUID,
    *,
    actor_user_id: UUID,
    now: datetime | None = None,
) -> Incident:
    """Mark an open incident as being worked on."""
    now = _require_aware(now or _utcnow(), "now")
    return await _change_status(
        session,
        incident_id,
        target=IncidentStatus.ACKNOWLEDGED,
        event=IncidentEvent(
            event_type=IncidentEventType.ACKNOWLEDGED.value,
            actor_user_id=actor_user_id,
            created_at=now,
        ),
        values_for=lambda i: {"acknowledged_at": max(now, _aware(i.started_at))},
    )


async def add_note(
    session: AsyncSession,
    incident_id: UUID,
    *,
    actor_user_id: UUID,
    message: str,
    now: datetime | None = None,
) -> IncidentEvent:
    """Add a note to the timeline. Resolved incidents accept notes too (post-mortems)."""
    text = message.strip()
    if not text:
        raise ValueError("A note cannot be empty")
    if len(text) > MAX_NOTE_LENGTH:
        raise ValueError(f"A note can be at most {MAX_NOTE_LENGTH} characters")
    await get_incident(session, incident_id)  # raises IncidentNotFound
    event = IncidentEvent(
        incident_id=incident_id,
        event_type=IncidentEventType.NOTE.value,
        message=text,
        actor_user_id=actor_user_id,
        created_at=now or _utcnow(),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def evaluate_monitor(
    session: AsyncSession,
    monitor_id: UUID,
    history: CheckHistory,
    *,
    rule: IncidentRule | None = None,
    now: datetime | None = None,
) -> Evaluation:
    """Look at the monitor's recent results and open or resolve its incident.

    Call this after every new check result. Safe to call repeatedly and from
    several workers at once: it never opens a second incident for a monitor.
    """
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None:
        return Evaluation(IncidentAction.NONE, "monitor not found")
    if not monitor.is_active:
        # A paused monitor isn't checked, so its old results must not open or
        # resolve anything. An incident already open stays open for a person to close.
        return Evaluation(IncidentAction.NONE, "monitor is paused")

    rule = rule or IncidentRule.for_monitor(
        check_interval_seconds=monitor.check_interval_seconds,
        retry_count=monitor.retry_count,
        retry_interval_seconds=monitor.retry_interval_seconds,
    )
    active = await get_active_incident(session, monitor.id)
    results = await history.recent_results(
        monitor.id, limit=max(DEFAULT_HISTORY_LIMIT, rule.min_history)
    )
    decision = decide(results, incident_open=active is not None, rule=rule)

    if decision.action is IncidentAction.OPEN:
        assert decision.since is not None  # decide() always dates an OPEN
        incident, created = await open_incident(
            session, monitor, started_at=decision.since, cause=decision.cause, now=now
        )
        if not created:
            return Evaluation(IncidentAction.NONE, "incident already open", incident)
        return Evaluation(IncidentAction.OPEN, decision.reason, incident)

    if decision.action is IncidentAction.RESOLVE and active is not None:
        try:
            incident = await resolve_incident(
                session,
                active.id,
                resolved_at=decision.since,
                message=f"Recovered: {decision.reason}",
                now=now,
            )
        except InvalidIncidentTransition:  # someone resolved it a moment ago
            return Evaluation(IncidentAction.NONE, "incident already resolved")
        return Evaluation(IncidentAction.RESOLVE, decision.reason, incident)

    return Evaluation(IncidentAction.NONE, decision.reason, active)
