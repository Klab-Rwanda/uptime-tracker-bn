import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.auth.errors import APIError
from app.incidents import service
from app.incidents.access import incident_not_found, load_incident, require_member
from app.incidents.alerts import AlertEvent, enqueue_incident_alerts
from app.incidents.dependencies import get_alert_store
from app.incidents.schemas import (
    IncidentDetailEnvelope,
    IncidentDetailResponse,
    IncidentEnvelope,
    IncidentEventEnvelope,
    IncidentEventResponse,
    IncidentListEnvelope,
    IncidentResponse,
    IncidentStatusFilter,
    NoteRequest,
    ResolveRequest,
)
from app.models.incident import Incident, IncidentEvent
from app.services.incident_logic import InvalidIncidentTransition
from app.services.notifications.delivery import DeliveryStore
from app.util.database import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/incidents", tags=["incidents"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]
AlertStore = Annotated[DeliveryStore, Depends(get_alert_store)]


def _incident(incident: Incident) -> IncidentResponse:
    return IncidentResponse(
        id=incident.id,
        organization_id=incident.organization_id,
        monitor_id=incident.monitor_id,
        status=incident.status,
        title=incident.title,
        cause=incident.cause,
        started_at=incident.started_at,
        acknowledged_at=incident.acknowledged_at,
        resolved_at=incident.resolved_at,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
    )


def _event(event: IncidentEvent) -> IncidentEventResponse:
    return IncidentEventResponse(
        id=event.id,
        type=event.event_type,
        message=event.message,
        actor_user_id=event.actor_user_id,
        created_at=event.created_at,
    )


def _conflict(exc: InvalidIncidentTransition) -> APIError:
    return APIError(409, "conflict", str(exc))


@router.get("", response_model=IncidentListEnvelope)
async def list_incidents(
    organization_id: Annotated[UUID, Query(description="Organization ID")],
    current: CurrentAuth,
    session: DatabaseSession,
    status_filter: Annotated[
        IncidentStatusFilter | None,
        Query(alias="status", description="open, acknowledged, resolved, or active (open + acknowledged)"),
    ] = None,
    monitor_id: Annotated[UUID | None, Query(description="Only this monitor's incidents")] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIST_LIMIT)] = 50,
) -> IncidentListEnvelope:
    await require_member(session, organization_id, current)
    incidents = await service.list_incidents(
        session,
        organization_id=organization_id,
        status=status_filter,
        monitor_id=monitor_id,
        limit=limit,
    )
    return IncidentListEnvelope(data=[_incident(i) for i in incidents])


@router.get("/{incident_id}", response_model=IncidentDetailEnvelope)
async def get_incident(
    incident_id: UUID, current: CurrentAuth, session: DatabaseSession
) -> IncidentDetailEnvelope:
    incident = await load_incident(session, incident_id, current, write=False)
    timeline = await service.get_timeline(session, incident.id)
    return IncidentDetailEnvelope(
        data=IncidentDetailResponse(
            **_incident(incident).model_dump(), timeline=[_event(e) for e in timeline]
        )
    )


@router.post("/{incident_id}/acknowledge", response_model=IncidentEnvelope)
async def acknowledge_incident(
    incident_id: UUID, current: CurrentAuth, session: DatabaseSession
) -> IncidentEnvelope:
    await load_incident(session, incident_id, current, write=True)
    try:
        incident = await service.acknowledge_incident(
            session, incident_id, actor_user_id=current.user.id
        )
    except InvalidIncidentTransition as exc:
        raise _conflict(exc) from exc
    except service.IncidentNotFound:
        raise incident_not_found() from None
    return IncidentEnvelope(data=_incident(incident))


@router.post("/{incident_id}/resolve", response_model=IncidentEnvelope)
async def resolve_incident(
    incident_id: UUID,
    current: CurrentAuth,
    session: DatabaseSession,
    alerts: AlertStore,
    request: ResolveRequest | None = None,
) -> IncidentEnvelope:
    await load_incident(session, incident_id, current, write=True)
    note = (request.note or "").strip() if request else ""
    try:
        incident = await service.resolve_incident(
            session, incident_id, actor_user_id=current.user.id, message=note or None
        )
    except InvalidIncidentTransition as exc:
        raise _conflict(exc) from exc
    except service.IncidentNotFound:
        raise incident_not_found() from None

    # Build the answer now: a rollback below would expire `incident`, and
    # reloading it lazily isn't possible in async code
    response = IncidentEnvelope(data=_incident(incident))
    try:
        await enqueue_incident_alerts(session, alerts, incident, AlertEvent.RESOLVED)
    except Exception:
        # The incident IS resolved; only the alert failed to queue. Log it rather
        # than tell the user their resolve failed.
        await session.rollback()
        logger.exception("could not queue resolved alerts", extra={"incident_id": str(incident_id)})
    return response


@router.post(
    "/{incident_id}/notes",
    response_model=IncidentEventEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def add_note(
    incident_id: UUID, request: NoteRequest, current: CurrentAuth, session: DatabaseSession
) -> IncidentEventEnvelope:
    await load_incident(session, incident_id, current, write=True)
    try:
        event = await service.add_note(
            session, incident_id, actor_user_id=current.user.id, message=request.note
        )
    except ValueError as exc:
        raise APIError(422, "validation_error", str(exc)) from exc
    except service.IncidentNotFound:
        raise incident_not_found() from None
    return IncidentEventEnvelope(data=_event(event))
