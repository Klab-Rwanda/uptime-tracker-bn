from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser
from app.dashboard.schemas import (
    DashboardSummaryResponse,
    ProjectSummary,
    StatusCounts,
)
from app.models.monitor import Monitor
from app.organizations.service import _get_org_membership
from app.projects.models import Project


def _init_counts() -> dict[str, int]:
    return {"up": 0, "down": 0, "pending": 0, "paused": 0, "maintenance": 0}


async def get_dashboard_summary(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> DashboardSummaryResponse:
    await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )

    monitors_stmt = select(Monitor).where(Monitor.organization_id == organization_id)
    monitors_result = await session.execute(monitors_stmt)
    monitors = list(monitors_result.scalars().all())

    projects_stmt = (
        select(Project)
        .where(Project.organization_id == organization_id)
        .order_by(Project.created_at.asc())
    )
    projects_result = await session.execute(projects_stmt)
    projects = list(projects_result.scalars().all())

    overall_counts = _init_counts()
    project_counts: dict[UUID | None, dict[str, int]] = {p.id: _init_counts() for p in projects}

    for monitor in monitors:
        status_key = monitor.current_status.lower()
        if status_key in overall_counts:
            overall_counts[status_key] += 1
        else:
            overall_counts["pending"] += 1

        if monitor.project_id in project_counts:
            if status_key in project_counts[monitor.project_id]:
                project_counts[monitor.project_id][status_key] += 1
            else:
                project_counts[monitor.project_id]["pending"] += 1

    project_summaries = []
    for project in projects:
        counts = project_counts[project.id]
        total_p_monitors = sum(counts.values())
        project_summaries.append(
            ProjectSummary(
                id=project.id,
                name=project.name,
                total_monitors=total_p_monitors,
                status_counts=StatusCounts(**counts),
            )
        )

    return DashboardSummaryResponse(
        organization_id=organization_id,
        total_monitors=len(monitors),
        status_counts=StatusCounts(**overall_counts),
        total_projects=len(projects),
        projects=project_summaries,
    )
