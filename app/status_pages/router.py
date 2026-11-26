from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.status_pages import service
from app.status_pages.schemas import (
    PublicStatusPageEnvelope,
    StatusPageCreate,
    StatusPageEnvelope,
    StatusPageListEnvelope,
    StatusPageUpdate,
)
from app.util.database import get_db_session

router = APIRouter(prefix="/v1/status-pages", tags=["status-pages"])
public_router = APIRouter(tags=["public status pages"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]

# Visitors pile onto a status page during an outage: let browsers and proxies
# reuse a response for a short while
PUBLIC_CACHE_SECONDS = 30


@router.post("", response_model=StatusPageEnvelope, status_code=status.HTTP_201_CREATED)
async def create_status_page(
    request: StatusPageCreate, current: CurrentAuth, session: DatabaseSession
) -> StatusPageEnvelope:
    page = await service.create_page(session, data=request, current=current)
    return StatusPageEnvelope(data=service.to_response(page))


@router.get("", response_model=StatusPageListEnvelope)
async def list_status_pages(
    organization_id: Annotated[UUID, Query(description="Organization ID")],
    current: CurrentAuth,
    session: DatabaseSession,
) -> StatusPageListEnvelope:
    pages = await service.list_pages(session, organization_id=organization_id, current=current)
    return StatusPageListEnvelope(data=[service.to_response(p) for p in pages])


@router.get("/{page_id}", response_model=StatusPageEnvelope)
async def get_status_page(
    page_id: UUID, current: CurrentAuth, session: DatabaseSession
) -> StatusPageEnvelope:
    page = await service.get_page(session, page_id=page_id, current=current)
    return StatusPageEnvelope(data=service.to_response(page))


@router.patch("/{page_id}", response_model=StatusPageEnvelope)
async def update_status_page(
    page_id: UUID, request: StatusPageUpdate, current: CurrentAuth, session: DatabaseSession
) -> StatusPageEnvelope:
    page = await service.update_page(session, page_id=page_id, data=request, current=current)
    return StatusPageEnvelope(data=service.to_response(page))


@router.delete("/{page_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_status_page(page_id: UUID, current: CurrentAuth, session: DatabaseSession) -> None:
    await service.delete_page(session, page_id=page_id, current=current)


@public_router.get("/status/{slug}", response_model=PublicStatusPageEnvelope)
async def public_status_page(
    slug: str, response: Response, session: DatabaseSession
) -> PublicStatusPageEnvelope:
    """The public status page. No login: shows only names and statuses."""
    page = await service.get_public_page(session, slug)
    response.headers["Cache-Control"] = f"public, max-age={PUBLIC_CACHE_SECONDS}"
    return PublicStatusPageEnvelope(data=page)
