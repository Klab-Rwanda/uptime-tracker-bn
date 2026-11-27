from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record
from app.auth.api_key_access import require_scope
from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.maintenance.models import MaintenanceWindow, MaintenanceWindowMonitor
from app.maintenance.schemas import (
    MaintenanceWindowCreate,
    MaintenanceWindowResponse,
    MaintenanceWindowUpdate,
    WindowState,
    check_times,
)
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.organizations.service import _get_org_membership

READ_SCOPE = "maintenance:read"
WRITE_SCOPE = "maintenance:write"
WRITE_ROLES = ("owner", "admin", "editor")
ADMIN_ROLES = ("owner", "admin")
DEFAULT_LIMIT = 100


def as_utc(value: datetime) -> datetime:
    """SQLite gives back naive datetimes; everything is stored in UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def window_not_found() -> APIError:
    return APIError(404, "maintenance_window_not_found", "Maintenance window not found")


def state_of(window: MaintenanceWindow, now: datetime) -> WindowState:
    if now < as_utc(window.starts_at):
        return WindowState.UPCOMING
    if now < as_utc(window.ends_at):
        return WindowState.ACTIVE
    return WindowState.PAST


def to_response(window: MaintenanceWindow, now: datetime) -> MaintenanceWindowResponse:
    return MaintenanceWindowResponse(
        id=window.id,
        organization_id=window.organization_id,
        name=window.name,
        starts_at=as_utc(window.starts_at),
        ends_at=as_utc(window.ends_at),
        state=state_of(window, now),
        monitor_ids=sorted((m.monitor_id for m in window.monitors), key=str),
        suppress_notifications=window.suppress_notifications,
        created_by_user_id=window.created_by_user_id,
        created_at=as_utc(window.created_at),
        updated_at=as_utc(window.updated_at),
    )


# --- access ------------------------------------------------------------------------


async def _membership_role(
    session: AsyncSession, organization_id: UUID, current: AuthenticatedUser
) -> str:
    _, membership = await _get_org_membership(
        session, organization_id=organization_id, user_id=current.user.id
    )
    return membership.role


async def _load_for(
    session: AsyncSession,
    window_id: UUID,
    current: AuthenticatedUser,
    *,
    roles: tuple[str, ...],
    message: str,
) -> MaintenanceWindow:
    """The window, if `current` has one of `roles` in its organization.
    Windows of other organizations answer like missing ones."""
    require_scope(current, WRITE_SCOPE)
    window = await session.get(MaintenanceWindow, window_id)
    if window is None:
        raise window_not_found()
    try:
        role = await _membership_role(session, window.organization_id, current)
    except APIError as exc:
        if exc.status_code == 404:
            raise window_not_found() from None
        raise
    if role not in roles:
        raise APIError(403, "forbidden", message)
    return window


async def _check_monitors(
    session: AsyncSession, organization_id: UUID, monitor_ids: list[UUID]
) -> None:
    """Every monitor must belong to the window's organization."""
    found = set(
        (
            await session.scalars(
                select(Monitor.id).where(
                    Monitor.organization_id == organization_id, Monitor.id.in_(monitor_ids)
                )
            )
        ).all()
    )
    if found != set(monitor_ids):
        raise APIError(
            404, "monitor_not_found", "One or more monitors do not exist in this organization"
        )


def _must_end_in_future(ends_at: datetime, now: datetime) -> None:
    if ends_at <= now:
        raise APIError(422, "validation_error", "A maintenance window must end in the future")


# --- management ----------------------------------------------------------------------


async def create_window(
    session: AsyncSession,
    *,
    data: MaintenanceWindowCreate,
    current: AuthenticatedUser,
    now: datetime | None = None,
) -> MaintenanceWindowResponse:
    now = now or datetime.now(UTC)
    require_scope(current, WRITE_SCOPE)
    role = await _membership_role(session, data.organization_id, current)
    if role not in WRITE_ROLES:
        raise APIError(
            403, "forbidden", "An editor role or higher is required to plan maintenance"
        )
    starts_at, ends_at = as_utc(data.starts_at), as_utc(data.ends_at)
    _must_end_in_future(ends_at, now)
    await _check_monitors(session, data.organization_id, data.monitor_ids)

    window = MaintenanceWindow(
        id=uuid4(),
        organization_id=data.organization_id,
        name=data.name,
        starts_at=starts_at,
        ends_at=ends_at,
        suppress_notifications=data.suppress_notifications,
        created_by_user_id=current.user.id,
        monitors=[MaintenanceWindowMonitor(monitor_id=m) for m in data.monitor_ids],
    )
    session.add(window)
    record(
        session,
        current,
        organization_id=window.organization_id,
        action="maintenance_window.created",
        target_type="maintenance_window",
        target_id=window.id,
        details={
            "name": window.name,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        },
    )
    await session.commit()
    await session.refresh(window)
    return to_response(window, now)


async def list_windows(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
    state: WindowState | None = None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> list[MaintenanceWindowResponse]:
    now = now or datetime.now(UTC)
    require_scope(current, READ_SCOPE)
    await _membership_role(session, organization_id, current)

    stmt = select(MaintenanceWindow).where(MaintenanceWindow.organization_id == organization_id)
    if state is WindowState.UPCOMING:
        stmt = stmt.where(MaintenanceWindow.starts_at > now).order_by(
            MaintenanceWindow.starts_at
        )
    elif state is WindowState.ACTIVE:
        stmt = stmt.where(
            MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at > now
        ).order_by(MaintenanceWindow.starts_at)
    elif state is WindowState.PAST:
        stmt = stmt.where(MaintenanceWindow.ends_at <= now).order_by(
            MaintenanceWindow.starts_at.desc()
        )
    else:
        stmt = stmt.order_by(MaintenanceWindow.starts_at.desc())
    windows = (await session.scalars(stmt.order_by(MaintenanceWindow.id).limit(limit))).all()
    return [to_response(w, now) for w in windows]


async def update_window(
    session: AsyncSession,
    *,
    window_id: UUID,
    data: MaintenanceWindowUpdate,
    current: AuthenticatedUser,
    now: datetime | None = None,
) -> MaintenanceWindowResponse:
    now = now or datetime.now(UTC)
    window = await _load_for(
        session,
        window_id,
        current,
        roles=WRITE_ROLES,
        message="An editor role or higher is required to change maintenance",
    )
    if data.starts_at is not None or data.ends_at is not None:
        starts_at = as_utc(data.starts_at or window.starts_at)
        ends_at = as_utc(data.ends_at or window.ends_at)
        try:
            check_times(starts_at, ends_at)
        except ValueError as exc:
            raise APIError(422, "validation_error", str(exc)) from None
        _must_end_in_future(ends_at, now)
        window.starts_at, window.ends_at = starts_at, ends_at
    if data.monitor_ids is not None:
        await _check_monitors(session, window.organization_id, data.monitor_ids)
        window.monitors.clear()
        await session.flush()
        window.monitors.extend(MaintenanceWindowMonitor(monitor_id=m) for m in data.monitor_ids)
    if data.name is not None:
        window.name = data.name
    if data.suppress_notifications is not None:
        window.suppress_notifications = data.suppress_notifications
    window.updated_at = now
    record(
        session,
        current,
        organization_id=window.organization_id,
        action="maintenance_window.updated",
        target_type="maintenance_window",
        target_id=window.id,
        details={"fields": sorted(data.model_fields_set)},
    )
    await session.commit()
    await session.refresh(window)
    return to_response(window, now)


async def delete_window(
    session: AsyncSession, *, window_id: UUID, current: AuthenticatedUser
) -> None:
    window = await _load_for(
        session,
        window_id,
        current,
        roles=ADMIN_ROLES,
        message="An owner or administrator role is required to delete maintenance",
    )
    record(
        session,
        current,
        organization_id=window.organization_id,
        action="maintenance_window.deleted",
        target_type="maintenance_window",
        target_id=window.id,
        details={"name": window.name},
    )
    await session.delete(window)
    await session.commit()


# --- used by status pages and alerts --------------------------------------------------


def _covering(monitor_ids: list[UUID], at: datetime) -> Select[UUID, MaintenanceWindow]:
    return (
        select(MaintenanceWindowMonitor.monitor_id, MaintenanceWindow)
        .join(MaintenanceWindow, MaintenanceWindow.id == MaintenanceWindowMonitor.window_id)
        .where(
            MaintenanceWindowMonitor.monitor_id.in_(monitor_ids),
            MaintenanceWindow.starts_at <= at,
            MaintenanceWindow.ends_at > at,
        )
    )


async def windows_overlapping(
    session: AsyncSession, monitor_ids: list[UUID], start: datetime, end: datetime
) -> list[tuple[MaintenanceWindow, set[UUID]]]:
    """Windows that cover any of these monitors at some point in [start, end),
    each with the subset of `monitor_ids` it covers, earliest start first."""
    if not monitor_ids:
        return []
    rows = await session.execute(
        select(MaintenanceWindowMonitor.monitor_id, MaintenanceWindow)
        .join(MaintenanceWindow, MaintenanceWindow.id == MaintenanceWindowMonitor.window_id)
        .where(
            MaintenanceWindowMonitor.monitor_id.in_(monitor_ids),
            MaintenanceWindow.starts_at < as_utc(end),
            MaintenanceWindow.ends_at > as_utc(start),
        )
        .order_by(MaintenanceWindow.starts_at, MaintenanceWindow.id)
    )
    found: dict[UUID, tuple[MaintenanceWindow, set[UUID]]] = {}
    for monitor_id, window in rows.all():
        found.setdefault(window.id, (window, set()))[1].add(monitor_id)
    return list(found.values())


async def _suppressing_window(
    session: AsyncSession, monitor_id: UUID, at: datetime
) -> MaintenanceWindow | None:
    """The suppressing window covering the monitor at `at` that ends last."""
    stmt = (
        _covering([monitor_id], as_utc(at))
        .where(MaintenanceWindow.suppress_notifications.is_(True))
        .order_by(MaintenanceWindow.ends_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    return None if row is None else row[1]


async def alerts_suppressed(
    session: AsyncSession, incident: Incident, *, resolved: bool, now: datetime
) -> bool:
    """Whether planned maintenance silences this alert.

    - "opened": silenced while a suppressing window covers the monitor. If the
      monitor is still down when the window ends, the worker's next check
      queues the alert then, so a real outage is never hidden for good.
    - "resolved": silenced only when the incident started AND ended inside
      the same window, so nobody gets "resolved" for an alert they never got.
    """
    if not resolved:
        return await _suppressing_window(session, incident.monitor_id, now) is not None
    window = await _suppressing_window(session, incident.monitor_id, incident.started_at)
    if window is None:
        return False
    ended = as_utc(incident.resolved_at) if incident.resolved_at else as_utc(now)
    return ended < as_utc(window.ends_at)
