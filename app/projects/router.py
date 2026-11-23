from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.projects import service
from app.projects.models import Project
from app.projects.schemas import (
    ProjectCreate,
    ProjectEnvelope,
    ProjectListEnvelope,
    ProjectResponse,
    ProjectUpdate,
)
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/projects", tags=["projects"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]


def _project_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        organization_id=project.organization_id,
        name=project.name,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.post(
    "",
    response_model=ProjectEnvelope,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    request: ProjectCreate,
    current: CurrentAuth,
    session: DatabaseSession,
) -> ProjectEnvelope:
    project = await service.create_project(
        session,
        organization_id=request.organization_id,
        name=request.name,
        current=current,
    )
    return ProjectEnvelope(data=_project_response(project))


@router.get("", response_model=ProjectListEnvelope)
async def list_projects(
    organization_id: Annotated[UUID, Query(description="Organization ID to filter projects")],
    current: CurrentAuth,
    session: DatabaseSession,
) -> ProjectListEnvelope:
    projects = await service.list_projects(
        session,
        organization_id=organization_id,
        current=current,
    )
    return ProjectListEnvelope(
        data=[_project_response(project) for project in projects]
    )


@router.get("/{project_id}", response_model=ProjectEnvelope)
async def get_project(
    project_id: UUID,
    current: CurrentAuth,
    session: DatabaseSession,
) -> ProjectEnvelope:
    project = await service.get_project(
        session,
        project_id=project_id,
        current=current,
    )
    return ProjectEnvelope(data=_project_response(project))


@router.patch("/{project_id}", response_model=ProjectEnvelope)
async def update_project(
    project_id: UUID,
    request: ProjectUpdate,
    current: CurrentAuth,
    session: DatabaseSession,
) -> ProjectEnvelope:
    project = await service.update_project(
        session,
        project_id=project_id,
        name=request.name,
        current=current,
    )
    return ProjectEnvelope(data=_project_response(project))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    current: CurrentAuth,
    session: DatabaseSession,
) -> Response:
    await service.delete_project(
        session,
        project_id=project_id,
        current=current,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
