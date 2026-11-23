from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.organizations.models import OrganizationMembership
from app.organizations.service import _get_org_membership
from app.projects.models import Project

WRITE_ROLES = ("owner", "admin", "editor")
ADMIN_ROLES = ("owner", "admin")


async def create_project(
    session: AsyncSession,
    *,
    organization_id: UUID,
    name: str,
    current: AuthenticatedUser,
) -> Project:
    _, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if membership.role not in WRITE_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An editor role or higher is required to create a project",
        )

    existing = await session.scalar(
        select(Project).where(
            Project.organization_id == organization_id,
            Project.name == name,
        )
    )
    if existing is not None:
        raise APIError(
            409,
            "conflict",
            "A project with this name already exists in the organization",
        )

    project = Project(
        organization_id=organization_id,
        name=name,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def list_projects(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> list[Project]:
    await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    stmt = (
        select(Project)
        .where(Project.organization_id == organization_id)
        .order_by(Project.created_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_project(
    session: AsyncSession,
    *,
    project_id: UUID,
    current: AuthenticatedUser,
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise APIError(404, "project_not_found", "Project not found")

    await _get_org_membership(
        session,
        organization_id=project.organization_id,
        user_id=current.user.id,
    )
    return project


async def update_project(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    current: AuthenticatedUser,
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise APIError(404, "project_not_found", "Project not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=project.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in WRITE_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An editor role or higher is required to update a project",
        )

    existing = await session.scalar(
        select(Project).where(
            Project.organization_id == project.organization_id,
            Project.name == name,
            Project.id != project_id,
        )
    )
    if existing is not None:
        raise APIError(
            409,
            "conflict",
            "A project with this name already exists in the organization",
        )

    project.name = name
    await session.commit()
    await session.refresh(project)
    return project


async def delete_project(
    session: AsyncSession,
    *,
    project_id: UUID,
    current: AuthenticatedUser,
) -> None:
    project = await session.get(Project, project_id)
    if project is None:
        raise APIError(404, "project_not_found", "Project not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=project.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in ADMIN_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An owner or administrator role is required to delete a project",
        )

    await session.delete(project)
    await session.commit()
