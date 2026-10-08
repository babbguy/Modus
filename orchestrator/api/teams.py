"""
Modus — Teams Router
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import AuditLog, Team
from orchestrator.db.session import get_session

router = APIRouter()


class TeamResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: Optional[str] = None
    department: Optional[str] = None
    created_at: datetime


class TeamCreateRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = None
    department: Optional[str] = None
    parent_id: Optional[str] = None


@router.get("/teams/departments", tags=["teams"])
async def list_departments(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Return distinct department names with team counts."""
    identity.assert_permission("teams:read")
    q = (
        select(Team.department, func.count(Team.id).label("team_count"))
        .where(Team.deleted_at.is_(None), Team.department.isnot(None))
        .group_by(Team.department)
        .order_by(Team.department)
    )
    rows = (await db.execute(q)).all()
    return [{"department": row[0], "team_count": row[1]} for row in rows]


@router.get("/teams", response_model=list[TeamResponse], tags=["teams"])
async def list_teams(
    department: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[TeamResponse]:
    identity.assert_permission("teams:read")
    q = select(Team).where(Team.deleted_at.is_(None))
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(Team.id.in_(identity.team_ids))
    if department:
        q = q.where(Team.department == department)
    rows = (await db.execute(
        q.order_by(Team.name).limit(limit).offset(offset)
    )).scalars().all()
    return [TeamResponse(id=str(t.id), slug=t.slug, name=t.name,
                          description=t.description, department=t.department,
                          created_at=t.created_at)
            for t in rows]


@router.post("/teams", response_model=TeamResponse, status_code=201, tags=["teams"])
async def create_team(
    body: TeamCreateRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> TeamResponse:
    identity.assert_permission("teams:write")
    existing = (await db.execute(
        select(Team).where(Team.slug == body.slug, Team.deleted_at.is_(None))
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Team '{body.slug}' already exists.")

    # Validate parent team if specified
    if body.parent_id:
        parent = (await db.execute(
            select(Team).where(Team.id == body.parent_id, Team.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not parent:
            raise HTTPException(404, "Parent team not found.")

    team = Team(slug=body.slug, name=body.name, description=body.description,
                department=body.department, parent_id=body.parent_id)
    db.add(team)
    await db.flush()
    db.add(AuditLog(actor_id=identity.actor_id,
                    actor_ip=request.client.host if request.client else None,
                    resource_type="team", resource_id=str(team.id),
                    action="created", after=body.model_dump()))
    return TeamResponse(id=str(team.id), slug=team.slug, name=team.name,
                        description=team.description, department=team.department,
                        created_at=team.created_at)
