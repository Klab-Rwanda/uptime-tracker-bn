from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key_access import require_scope
from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.organizations.service import _get_org_membership
from app.services.incident_logic import IncidentStatus
from app.services.status_logic import (
    MonitorStatus,
    calculate_overall_status,
    effective_status,
)
from app.status_pages import cache
from app.status_pages.models import StatusPage, StatusPageGroup, StatusPageMonitor
from app.status_pages.schemas import (
    SLUG_RE,
    GroupIn,
    GroupOut,
    PublicGroup,
    PublicIncident,
    PublicMonitor,
    PublicStatusPage,
    StatusPageCreate,
    StatusPageResponse,
    StatusPageUpdate,
)
from app.util.config import get_settings

READ_SCOPE = "status_pages:read"
WRITE_SCOPE = "status_pages:write"
WRITE_ROLES = ("owner", "admin", "editor")
ADMIN_ROLES = ("owner", "admin")

# How visitors read an incident's status
PUBLIC_INCIDENT_STATUS = {
    IncidentStatus.OPEN.value: "investigating",
    IncidentStatus.ACKNOWLEDGED.value: "identified",
}


def page_not_found() -> APIError:
    return APIError(404, "status_page_not_found", "Status page not found")


def public_url(slug: str) -> str:
    return f"{get_settings().app_frontend_url.rstrip('/')}/status/{slug}"


def to_response(page: StatusPage) -> StatusPageResponse:
    return StatusPageResponse(
        id=page.id,
        organization_id=page.organization_id,
        title=page.title,
        slug=page.slug,
        custom_domain=page.custom_domain,
        url=public_url(page.slug),
        groups=[
            GroupOut(name=g.name, monitor_ids=[m.monitor_id for m in g.monitors])
            for g in page.groups
        ],
        created_at=page.created_at,
        updated_at=page.updated_at,
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
    page_id: UUID,
    current: AuthenticatedUser,
    *,
    roles: tuple[str, ...] | None,
    message: str = "",
) -> StatusPage:
    """The page, if `current` is a member (and has one of `roles`, when given).
    Pages of other organizations answer like missing ones."""
    require_scope(current, READ_SCOPE if roles is None else WRITE_SCOPE)
    page = await session.get(StatusPage, page_id)
    if page is None:
        raise page_not_found()
    try:
        role = await _membership_role(session, page.organization_id, current)
    except APIError as exc:
        if exc.status_code == 404:
            raise page_not_found() from None
        raise
    if roles is not None and role not in roles:
        raise APIError(403, "forbidden", message)
    return page


# --- validation -----------------------------------------------------------------------


async def _check_monitors(
    session: AsyncSession, organization_id: UUID, groups: list[GroupIn]
) -> None:
    """Every monitor on the page must belong to the page's organization."""
    wanted = {m for g in groups for m in g.monitor_ids}
    if not wanted:
        return
    found = set(
        (
            await session.scalars(
                select(Monitor.id).where(
                    Monitor.organization_id == organization_id, Monitor.id.in_(wanted)
                )
            )
        ).all()
    )
    if found != wanted:
        raise APIError(
            404, "monitor_not_found", "One or more monitors do not exist in this organization"
        )


async def _check_unique(
    session: AsyncSession, *, slug: str | None, domain: str | None, exclude: UUID | None
) -> None:
    for column, value, what in (
        (StatusPage.slug, slug, "slug"),
        (StatusPage.custom_domain, domain, "custom domain"),
    ):
        if value is None:
            continue
        stmt = select(StatusPage.id).where(column == value)
        if exclude is not None:
            stmt = stmt.where(StatusPage.id != exclude)
        if await session.scalar(stmt) is not None:
            raise APIError(409, "conflict", f"This {what} is already taken")


def _build_groups(groups: list[GroupIn]) -> list[StatusPageGroup]:
    return [
        StatusPageGroup(
            name=g.name,
            position=gi,
            monitors=[
                StatusPageMonitor(monitor_id=mid, position=mi)
                for mi, mid in enumerate(g.monitor_ids)
            ],
        )
        for gi, g in enumerate(groups)
    ]


async def _commit(session: AsyncSession, page: StatusPage) -> StatusPage:
    try:
        await session.commit()
    except IntegrityError as exc:  # two requests took the same slug/domain at once
        await session.rollback()
        raise APIError(409, "conflict", "This slug or custom domain is already taken") from exc
    await session.refresh(page)
    return page


# --- management (logged-in users) -------------------------------------------------------


async def create_page(
    session: AsyncSession, redis: Redis, *, data: StatusPageCreate, current: AuthenticatedUser
) -> StatusPage:
    require_scope(current, WRITE_SCOPE)
    role = await _membership_role(session, data.organization_id, current)
    if role not in WRITE_ROLES:
        raise APIError(403, "forbidden", "An editor role or higher is required to create a status page")
    await _check_monitors(session, data.organization_id, data.groups)
    await _check_unique(session, slug=data.slug, domain=data.custom_domain, exclude=None)

    page = StatusPage(
        organization_id=data.organization_id,
        title=data.title,
        slug=data.slug,
        custom_domain=data.custom_domain,
        groups=_build_groups(data.groups),
    )
    session.add(page)
    page = await _commit(session, page)
    await cache.forget(redis, page.slug)  # the slug may be cached as "not found"
    return page


async def list_pages(
    session: AsyncSession, *, organization_id: UUID, current: AuthenticatedUser
) -> list[StatusPage]:
    require_scope(current, READ_SCOPE)
    await _membership_role(session, organization_id, current)
    result = await session.scalars(
        select(StatusPage)
        .where(StatusPage.organization_id == organization_id)
        .order_by(StatusPage.title, StatusPage.id)
    )
    return list(result.all())


async def get_page(
    session: AsyncSession, *, page_id: UUID, current: AuthenticatedUser
) -> StatusPage:
    return await _load_for(session, page_id, current, roles=None)


async def update_page(
    session: AsyncSession,
    redis: Redis,
    *,
    page_id: UUID,
    data: StatusPageUpdate,
    current: AuthenticatedUser,
) -> StatusPage:
    page = await _load_for(
        session,
        page_id,
        current,
        roles=WRITE_ROLES,
        message="An editor role or higher is required to update a status page",
    )
    old_slug = page.slug
    sent = data.model_fields_set
    for required in ("title", "slug"):
        if required in sent and getattr(data, required) is None:
            raise APIError(422, "validation_error", f"{required} cannot be empty")

    await _check_unique(
        session,
        slug=data.slug if "slug" in sent else None,
        domain=data.custom_domain if "custom_domain" in sent else None,
        exclude=page.id,
    )
    if data.groups is not None:
        await _check_monitors(session, page.organization_id, data.groups)

    if data.title is not None:
        page.title = data.title
    if data.slug is not None:
        page.slug = data.slug
    if "custom_domain" in sent:
        page.custom_domain = data.custom_domain
    if data.groups is not None:
        # Remove the old groups first, so their positions are free again
        page.groups.clear()
        await session.flush()
        page.groups.extend(_build_groups(data.groups))
    page = await _commit(session, page)
    await cache.forget(redis, old_slug, page.slug)
    return page


async def delete_page(
    session: AsyncSession, redis: Redis, *, page_id: UUID, current: AuthenticatedUser
) -> None:
    page = await _load_for(
        session,
        page_id,
        current,
        roles=ADMIN_ROLES,
        message="An owner or administrator role is required to delete a status page",
    )
    slug = page.slug
    await session.delete(page)
    await session.commit()
    await cache.forget(redis, slug)


# --- the public page (no login) -------------------------------------------------------


def _shown_status(monitor: Monitor) -> MonitorStatus:
    try:
        return effective_status(monitor.current_status, monitor.is_active)
    except ValueError:
        # An unexpected stored value must never break the public page
        return MonitorStatus.PENDING


async def get_public_page_cached(
    session: AsyncSession, redis: Redis, slug: str
) -> tuple[PublicStatusPage, bool]:
    """The public page and whether it came from the cache."""
    slug = slug.lower()
    if not SLUG_RE.fullmatch(slug):
        raise page_not_found()
    cached = await cache.read(redis, slug)
    if isinstance(cached, PublicStatusPage):
        return cached, True
    if cached is cache.MISSING:
        raise page_not_found()

    try:
        built = await get_public_page(session, slug)
    except APIError as exc:
        if exc.status_code == 404:
            await cache.store_missing(redis, slug)
        raise
    await cache.store(redis, slug, built)
    return built, False


async def get_public_page(session: AsyncSession, slug: str) -> PublicStatusPage:
    slug = slug.lower()
    if not SLUG_RE.fullmatch(slug):
        raise page_not_found()
    page = await session.scalar(select(StatusPage).where(StatusPage.slug == slug))
    if page is None:
        raise page_not_found()

    monitor_ids = [m.monitor_id for g in page.groups for m in g.monitors]
    monitors: dict[UUID, Monitor] = {}
    incidents: list[Incident] = []
    if monitor_ids:
        rows = await session.scalars(select(Monitor).where(Monitor.id.in_(monitor_ids)))
        monitors = {m.id: m for m in rows.all()}
        incidents = list(
            (
                await session.scalars(
                    select(Incident)
                    .where(
                        Incident.monitor_id.in_(monitor_ids),
                        Incident.status != IncidentStatus.RESOLVED.value,
                    )
                    .order_by(Incident.started_at.desc())
                )
            ).all()
        )

    groups: list[PublicGroup] = []
    every_status: list[MonitorStatus] = []
    for group in page.groups:
        shown = [
            (monitors[m.monitor_id].name, _shown_status(monitors[m.monitor_id]))
            for m in group.monitors
            if m.monitor_id in monitors
        ]
        statuses = [status for _, status in shown]
        every_status.extend(statuses)
        groups.append(
            PublicGroup(
                name=group.name,
                status=calculate_overall_status(statuses).value,
                monitors=[PublicMonitor(name=name, status=status.value) for name, status in shown],
            )
        )

    return PublicStatusPage(
        title=page.title,
        overall_status=calculate_overall_status(every_status).value,
        groups=groups,
        active_incidents=[
            PublicIncident(
                title=incident.title,
                status=PUBLIC_INCIDENT_STATUS.get(incident.status, "investigating"),
                started_at=incident.started_at,
            )
            for incident in incidents
        ],
        generated_at=datetime.now(UTC),
    )
