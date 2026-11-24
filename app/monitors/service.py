import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.models.monitor import Monitor
from app.monitors.schemas import MonitorCreate, MonitorResponse, MonitorUpdate
from app.organizations.service import _get_org_membership
from app.projects.models import Project

WRITE_ROLES = ("owner", "admin", "editor")
ADMIN_ROLES = ("owner", "admin")


def _deserialize_list(raw: str | None) -> list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [raw]


def to_monitor_response(monitor: Monitor) -> MonitorResponse:
    accepted_codes = _deserialize_list(monitor.expected_status_codes)
    regions = _deserialize_list(monitor.regions)
    channels = _deserialize_list(monitor.notification_channel_ids)

    return MonitorResponse(
        id=monitor.id,
        organization_id=monitor.organization_id,
        project_id=monitor.project_id,
        name=monitor.name,
        type=monitor.monitor_type,
        target=monitor.target,
        method=monitor.method,
        status=monitor.current_status,
        interval_seconds=monitor.check_interval_seconds,
        timeout_seconds=monitor.timeout_seconds,
        retries=monitor.retry_count,
        retry_interval_seconds=monitor.retry_interval_seconds,
        accepted_status_codes=accepted_codes,
        expected_keyword=monitor.keyword,
        regions=regions,
        notification_channel_ids=channels,
        is_active=monitor.is_active,
        created_at=monitor.created_at,
        updated_at=monitor.updated_at,
    )


async def create_monitor(
    session: AsyncSession,
    *,
    data: MonitorCreate,
    current: AuthenticatedUser,
) -> Monitor:
    _, membership = await _get_org_membership(
        session,
        organization_id=data.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in WRITE_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An editor role or higher is required to create a monitor",
        )

    if data.project_id is not None:
        project = await session.get(Project, data.project_id)
        if project is None or project.organization_id != data.organization_id:
            raise APIError(
                404,
                "project_not_found",
                "The specified project does not exist in this organization",
            )

    now = datetime.now(UTC)
    monitor = Monitor(
        organization_id=data.organization_id,
        project_id=data.project_id,
        name=data.name,
        monitor_type=data.type,
        target=data.target,
        method=data.method,
        timeout_seconds=data.timeout_seconds,
        retry_count=data.retries,
        retry_interval_seconds=data.retry_interval_seconds,
        check_interval_seconds=data.interval_seconds,
        expected_status_codes=(
            json.dumps(data.accepted_status_codes)
            if data.accepted_status_codes is not None
            else None
        ),
        keyword=data.expected_keyword,
        regions=json.dumps(data.regions) if data.regions is not None else None,
        notification_channel_ids=(
            json.dumps(data.notification_channel_ids)
            if data.notification_channel_ids is not None
            else None
        ),
        is_active=True,
        current_status="pending",
        next_check_at=now,
    )
    session.add(monitor)
    await session.commit()
    await session.refresh(monitor)
    return monitor


async def list_monitors(
    session: AsyncSession,
    *,
    organization_id: UUID,
    project_id: UUID | None = None,
    status: str | None = None,
    monitor_type: str | None = None,
    current: AuthenticatedUser,
) -> list[Monitor]:
    await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )

    stmt = select(Monitor).where(Monitor.organization_id == organization_id)
    if project_id is not None:
        stmt = stmt.where(Monitor.project_id == project_id)
    if status is not None:
        stmt = stmt.where(Monitor.current_status == status.lower())
    if monitor_type is not None:
        stmt = stmt.where(Monitor.monitor_type == monitor_type.lower())

    stmt = stmt.order_by(Monitor.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_monitor(
    session: AsyncSession,
    *,
    monitor_id: UUID,
    current: AuthenticatedUser,
) -> Monitor:
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None:
        raise APIError(404, "monitor_not_found", "Monitor not found")

    await _get_org_membership(
        session,
        organization_id=monitor.organization_id,
        user_id=current.user.id,
    )
    return monitor


async def update_monitor(
    session: AsyncSession,
    *,
    monitor_id: UUID,
    data: MonitorUpdate,
    current: AuthenticatedUser,
) -> Monitor:
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None:
        raise APIError(404, "monitor_not_found", "Monitor not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=monitor.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in WRITE_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An editor role or higher is required to update a monitor",
        )

    if data.project_id is not None:
        project = await session.get(Project, data.project_id)
        if project is None or project.organization_id != monitor.organization_id:
            raise APIError(
                404,
                "project_not_found",
                "The specified project does not exist in this organization",
            )
        monitor.project_id = data.project_id

    if data.name is not None:
        monitor.name = data.name
    if data.target is not None:
        monitor.target = data.target
    if data.method is not None:
        monitor.method = data.method
    if data.interval_seconds is not None:
        monitor.check_interval_seconds = data.interval_seconds
    if data.timeout_seconds is not None:
        monitor.timeout_seconds = data.timeout_seconds
    if data.retries is not None:
        monitor.retry_count = data.retries
    if data.retry_interval_seconds is not None:
        monitor.retry_interval_seconds = data.retry_interval_seconds
    if data.accepted_status_codes is not None:
        monitor.expected_status_codes = json.dumps(data.accepted_status_codes)
    if data.expected_keyword is not None:
        monitor.keyword = data.expected_keyword
    if data.regions is not None:
        monitor.regions = json.dumps(data.regions)
    if data.notification_channel_ids is not None:
        monitor.notification_channel_ids = json.dumps(data.notification_channel_ids)

    if data.active is not None:
        monitor.is_active = data.active
        if not data.active:
            monitor.current_status = "paused"
        elif monitor.current_status == "paused":
            monitor.current_status = "pending"

    await session.commit()
    await session.refresh(monitor)
    return monitor


async def delete_monitor(
    session: AsyncSession,
    *,
    monitor_id: UUID,
    current: AuthenticatedUser,
) -> None:
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None:
        raise APIError(404, "monitor_not_found", "Monitor not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=monitor.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in ADMIN_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An owner or administrator role is required to delete a monitor",
        )

    await session.delete(monitor)
    await session.commit()


async def pause_monitor(
    session: AsyncSession,
    *,
    monitor_id: UUID,
    current: AuthenticatedUser,
) -> Monitor:
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None:
        raise APIError(404, "monitor_not_found", "Monitor not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=monitor.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in WRITE_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An editor role or higher is required to pause a monitor",
        )

    monitor.is_active = False
    monitor.current_status = "paused"
    await session.commit()
    await session.refresh(monitor)
    return monitor


async def resume_monitor(
    session: AsyncSession,
    *,
    monitor_id: UUID,
    current: AuthenticatedUser,
) -> Monitor:
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None:
        raise APIError(404, "monitor_not_found", "Monitor not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=monitor.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in WRITE_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An editor role or higher is required to resume a monitor",
        )

    monitor.is_active = True
    monitor.current_status = "pending"
    await session.commit()
    await session.refresh(monitor)
    return monitor


async def test_monitor(
    session: AsyncSession,
    *,
    monitor_id: UUID,
    current: AuthenticatedUser,
) -> dict[str, str]:
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None:
        raise APIError(404, "monitor_not_found", "Monitor not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=monitor.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in WRITE_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An editor role or higher is required to test a monitor",
        )

    return {
        "monitor_id": str(monitor.id),
        "status": "queued",
        "message": f"Test check for monitor '{monitor.name}' enqueued successfully",
    }
