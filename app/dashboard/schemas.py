from uuid import UUID

from pydantic import BaseModel


class StatusCounts(BaseModel):
    up: int = 0
    down: int = 0
    pending: int = 0
    paused: int = 0
    maintenance: int = 0


class ProjectSummary(BaseModel):
    id: UUID
    name: str
    total_monitors: int
    status_counts: StatusCounts


class DashboardSummaryResponse(BaseModel):
    organization_id: UUID
    total_monitors: int
    status_counts: StatusCounts
    total_projects: int
    projects: list[ProjectSummary]


class DashboardSummaryEnvelope(BaseModel):
    data: DashboardSummaryResponse
