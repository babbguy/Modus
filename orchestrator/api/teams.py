"""
Modus — Teams Router

GET    /api/v1/teams/departments       — distinct departments with team counts
GET    /api/v1/teams                   — list teams (?include_deleted=true for admins)
POST   /api/v1/teams                   — create a team
GET    /api/v1/teams/{team_id}         — full detail (budgets, parent, children, counts)
PATCH  /api/v1/teams/{team_id}         — partial update
DELETE /api/v1/teams/{team_id}         — soft delete (apps / child teams guarded)
POST   /api/v1/teams/{team_id}/restore — undo a soft delete

Deleting a team never removes usage, cost, alert or audit history. Those rows
keep the team id they were recorded under.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import (
    App,
    AppTopology,
    AuditLog,
    CostCenter,
    GovernancePolicy,
    RealTimeSpend,
    SessionBudget,
    Team,
    TeamMembership,
    Threshold,
)
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()

SLUG_PATTERN = r"^[a-z0-9][a-z0-9\-_]*$"
BudgetDuration = Literal["daily", "monthly", "30d", "1h"]
_MONEY_FIELDS = ("max_budget_usd", "budget_monthly_usd", "budget_quarterly_usd")


# ── Schemas ──────────────────────────────────────────────────────────────────

class TeamResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: Optional[str] = None
    department: Optional[str] = None
    parent_id: Optional[str] = None
    active_app_count: int = 0
    max_budget_usd: Optional[str] = None
    budget_monthly_usd: Optional[str] = None
    budget_quarterly_usd: Optional[str] = None
    created_at: datetime
    deleted_at: Optional[datetime] = None


class TeamRef(BaseModel):
    id: str
    slug: str
    name: str


class CostCenterRef(BaseModel):
    id: str
    code: str
    name: str


class TeamCounts(BaseModel):
    active_apps: int
    members: int
    policies: int
    child_teams: int


class TeamDetailResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: Optional[str] = None
    department: Optional[str] = None
    parent_id: Optional[str] = None
    parent: Optional[TeamRef] = None
    child_teams: list[TeamRef] = []
    max_budget_usd: Optional[str] = None
    budget_duration: Optional[str] = None
    budget_monthly_usd: Optional[str] = None
    budget_quarterly_usd: Optional[str] = None
    cost_center_id: Optional[str] = None
    cost_center: Optional[CostCenterRef] = None
    counts: TeamCounts
    has_registration_token: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


class TeamCreateRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = None
    department: Optional[str] = None
    parent_id: Optional[str] = None


class TeamUpdateRequest(BaseModel):
    """Partial update. Omitted fields are untouched; explicit null clears
    the nullable ones (description, department, parent, budgets, cost center)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=256)
    slug: Optional[str] = Field(None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    description: Optional[str] = Field(None, max_length=4000)
    department: Optional[str] = Field(None, max_length=256)
    parent_id: Optional[str] = None
    max_budget_usd: Optional[Decimal] = Field(None, ge=0, max_digits=18, decimal_places=8, allow_inf_nan=False)
    budget_monthly_usd: Optional[Decimal] = Field(None, ge=0, max_digits=18, decimal_places=8, allow_inf_nan=False)
    budget_quarterly_usd: Optional[Decimal] = Field(None, ge=0, max_digits=18, decimal_places=8, allow_inf_nan=False)
    budget_duration: Optional[BudgetDuration] = None
    cost_center_id: Optional[str] = None

    @field_validator("max_budget_usd", "budget_monthly_usd", "budget_quarterly_usd", mode="before")
    @classmethod
    def _no_float_money(cls, v: Any) -> Any:
        # Money travels as decimal strings (or ints); a JSON float has already
        # lost precision, so refuse it rather than guess.
        if isinstance(v, (float, bool)):
            raise ValueError('send money as a decimal string, e.g. "100.50"')
        return v

    @field_validator("name", "slug")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class TeamDeleteResult(BaseModel):
    id: str
    slug: str
    deleted_at: datetime
    apps_reassigned: int = 0
    apps_deactivated: int = 0
    child_teams_reassigned: int = 0
    registration_token_revoked: bool = False


class TeamRestoreResult(TeamDetailResponse):
    parent_cleared: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _money(v: Any) -> Optional[str]:
    if v is None:
        return None
    return format(Decimal(str(v)), "f")


def _iso(v: Optional[datetime]) -> Optional[str]:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat()


def _canon_id(raw: str, *, status_code: int, what: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code, f"{what} is not a valid id.")


def _snapshot(team: Team) -> dict:
    """JSON-safe copy of a team's editable state for AuditLog before/after."""
    return {
        "slug": team.slug,
        "name": team.name,
        "description": team.description,
        "department": team.department,
        "parent_id": team.parent_id,
        "max_budget_usd": _money(team.max_budget_usd),
        "budget_duration": team.budget_duration,
        "budget_monthly_usd": _money(team.budget_monthly_usd),
        "budget_quarterly_usd": _money(team.budget_quarterly_usd),
        "cost_center_id": team.cost_center_id,
        "deleted_at": _iso(team.deleted_at),
    }


def _audit(
    db: AsyncSession, request: Request, identity: Identity, team_id: str, action: str,
    *, resource_type: str = "team", resource_id: Optional[str] = None,
    before: Optional[dict] = None, after: Optional[dict] = None,
) -> None:
    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=team_id,
        resource_type=resource_type,
        resource_id=resource_id or team_id,
        action=action,
        before=before,
        after=after,
    ))


async def _get_live_team(db: AsyncSession, team_id: str) -> Team:
    tid = _canon_id(team_id, status_code=404, what="team_id")
    team = await db.get(Team, tid)
    if team is None or team.deleted_at is not None:
        raise HTTPException(404, "Team not found.")
    return team


async def _slug_owner(db: AsyncSession, slug: str, *, exclude_id: Optional[str] = None) -> Optional[Team]:
    """Team already holding `slug`. A soft-deleted team keeps its slug (the
    column is globally unique) so it can always be restored."""
    q = select(Team).where(Team.slug == slug)
    if exclude_id:
        q = q.where(Team.id != exclude_id)
    return (await db.execute(q)).scalars().first()


def _slug_conflict(owner: Team) -> HTTPException:
    if owner.deleted_at is not None:
        return HTTPException(
            409,
            f"Slug '{owner.slug}' is reserved by a deleted team ('{owner.name}'). "
            "Restore that team or choose another slug.",
        )
    return HTTPException(409, f"Team '{owner.slug}' already exists.")


async def _is_descendant(db: AsyncSession, candidate_id: str, ancestor_id: str) -> bool:
    """True if `ancestor_id` is `candidate_id` or one of its ancestors."""
    seen: set[str] = set()
    cur: Optional[str] = candidate_id
    while cur and cur not in seen:
        if cur == ancestor_id:
            return True
        seen.add(cur)
        row = await db.get(Team, cur)
        cur = row.parent_id if row else None
    return False


async def _require_live_target(
    db: AsyncSession, identity: Identity, raw_id: str, *, param: str,
) -> Team:
    tid = _canon_id(raw_id, status_code=422, what=param)
    target = await db.get(Team, tid)
    if target is None or target.deleted_at is not None:
        raise HTTPException(422, f"{param}: team {tid} does not exist or is deleted.")
    identity.assert_team_access(str(target.id))
    return target


async def _counts(db: AsyncSession, team_id: str) -> TeamCounts:
    apps = (await db.execute(
        select(func.count(App.id)).where(App.team_id == team_id, App.deleted_at.is_(None))
    )).scalar_one()
    members = (await db.execute(
        select(func.count(TeamMembership.id)).where(TeamMembership.team_id == team_id)
    )).scalar_one()
    policies = (await db.execute(
        select(func.count(GovernancePolicy.id)).where(GovernancePolicy.team_id == team_id)
    )).scalar_one()
    children = (await db.execute(
        select(func.count(Team.id)).where(Team.parent_id == team_id, Team.deleted_at.is_(None))
    )).scalar_one()
    return TeamCounts(active_apps=apps, members=members, policies=policies, child_teams=children)


async def _detail(db: AsyncSession, team: Team) -> TeamDetailResponse:
    parent = await db.get(Team, team.parent_id) if team.parent_id else None
    kids = (await db.execute(
        select(Team).where(Team.parent_id == team.id, Team.deleted_at.is_(None)).order_by(Team.name)
    )).scalars().all()
    cc = await db.get(CostCenter, team.cost_center_id) if team.cost_center_id else None
    return TeamDetailResponse(
        id=str(team.id), slug=team.slug, name=team.name,
        description=team.description, department=team.department,
        parent_id=team.parent_id,
        parent=TeamRef(id=str(parent.id), slug=parent.slug, name=parent.name) if parent else None,
        child_teams=[TeamRef(id=str(k.id), slug=k.slug, name=k.name) for k in kids],
        max_budget_usd=_money(team.max_budget_usd),
        budget_duration=team.budget_duration,
        budget_monthly_usd=_money(team.budget_monthly_usd),
        budget_quarterly_usd=_money(team.budget_quarterly_usd),
        cost_center_id=team.cost_center_id,
        cost_center=CostCenterRef(id=str(cc.id), code=cc.code, name=cc.name) if cc else None,
        counts=await _counts(db, str(team.id)),
        has_registration_token=bool(team.registration_token_hash),
        created_at=team.created_at,
        updated_at=team.updated_at,
        deleted_at=team.deleted_at,
    )


# ── Read ─────────────────────────────────────────────────────────────────────

@router.get("/teams/departments", tags=["teams"])
async def list_departments(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
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
    include_deleted: bool = Query(False, description="Include soft-deleted teams (platform admins only)."),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[TeamResponse]:
    identity.assert_permission("teams:read")
    if include_deleted and not identity.is_platform_admin:
        raise HTTPException(403, "include_deleted requires a platform admin.")
    q = select(Team)
    if not include_deleted:
        q = q.where(Team.deleted_at.is_(None))
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(Team.id.in_(identity.team_ids))
    if department:
        q = q.where(Team.department == department)
    rows = (await db.execute(
        q.order_by(Team.name, Team.id).limit(limit).offset(offset)
    )).scalars().all()

    counts: dict[str, int] = {}
    if rows:
        counts = {tid: n for tid, n in (await db.execute(
            select(App.team_id, func.count(App.id))
            .where(App.team_id.in_([t.id for t in rows]), App.deleted_at.is_(None))
            .group_by(App.team_id)
        )).all()}
    return [TeamResponse(
        id=str(t.id), slug=t.slug, name=t.name, description=t.description,
        department=t.department, parent_id=t.parent_id,
        active_app_count=counts.get(t.id, 0),
        max_budget_usd=_money(t.max_budget_usd),
        budget_monthly_usd=_money(t.budget_monthly_usd),
        budget_quarterly_usd=_money(t.budget_quarterly_usd),
        created_at=t.created_at, deleted_at=t.deleted_at,
    ) for t in rows]


@router.get("/teams/{team_id}", response_model=TeamDetailResponse, tags=["teams"])
async def get_team(
    team_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> TeamDetailResponse:
    identity.assert_permission("teams:read")
    team = await _get_live_team(db, team_id)
    identity.assert_team_access(str(team.id))
    return await _detail(db, team)


# ── Create ───────────────────────────────────────────────────────────────────

@router.post("/teams", response_model=TeamResponse, status_code=201, tags=["teams"])
async def create_team(
    body: TeamCreateRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> TeamResponse:
    identity.assert_permission("teams:write")
    owner = await _slug_owner(db, body.slug)
    if owner:
        raise _slug_conflict(owner)

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
                        parent_id=team.parent_id, created_at=team.created_at)


# ── Update ───────────────────────────────────────────────────────────────────

@router.patch("/teams/{team_id}", response_model=TeamDetailResponse, tags=["teams"])
async def update_team(
    team_id: str,
    body: TeamUpdateRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> TeamDetailResponse:
    identity.assert_permission("teams:write")
    team = await _get_live_team(db, team_id)
    identity.assert_team_access(str(team.id))

    sent = body.model_fields_set
    if not sent:
        raise HTTPException(422, "No fields to update.")
    for required in ("name", "slug"):
        if required in sent and not getattr(body, required):
            raise HTTPException(422, f"{required} cannot be null or blank.")

    before = _snapshot(team)

    if "slug" in sent and body.slug != team.slug:
        owner = await _slug_owner(db, body.slug, exclude_id=str(team.id))
        if owner:
            raise _slug_conflict(owner)
        team.slug = body.slug
    if "name" in sent:
        team.name = body.name
    if "description" in sent:
        team.description = body.description
    if "department" in sent:
        team.department = (body.department or "").strip() or None

    if "parent_id" in sent:
        if body.parent_id is None:
            team.parent_id = None
        else:
            parent = await _require_live_target(db, identity, body.parent_id, param="parent_id")
            if str(parent.id) == str(team.id):
                raise HTTPException(422, "parent_id: a team cannot be its own parent.")
            if await _is_descendant(db, str(parent.id), str(team.id)):
                raise HTTPException(
                    422, f"parent_id: '{parent.slug}' is a descendant of '{team.slug}'; "
                         "that would create a cycle in the team hierarchy.")
            team.parent_id = str(parent.id)

    for f in _MONEY_FIELDS:
        if f in sent:
            setattr(team, f, getattr(body, f))
    if "budget_duration" in sent:
        team.budget_duration = body.budget_duration

    if "cost_center_id" in sent:
        if body.cost_center_id is None:
            team.cost_center_id = None
        else:
            ccid = _canon_id(body.cost_center_id, status_code=422, what="cost_center_id")
            cc = await db.get(CostCenter, ccid)
            if cc is None or not cc.is_active:
                raise HTTPException(422, f"cost_center_id: cost center {ccid} not found or inactive.")
            team.cost_center_id = ccid

    await db.flush()
    after = _snapshot(team)
    changed = sorted(k for k in after if before[k] != after[k])
    if changed:
        _audit(db, request, identity, str(team.id), "updated",
               before={k: before[k] for k in changed}, after={k: after[k] for k in changed})
    await db.refresh(team)
    return await _detail(db, team)


# ── Delete ───────────────────────────────────────────────────────────────────

@router.delete("/teams/{team_id}", response_model=TeamDeleteResult, tags=["teams"])
async def delete_team(
    team_id: str,
    request: Request,
    reassign_to: Optional[str] = Query(None, description="Move this team's apps, policies and thresholds to this team."),
    cascade: bool = Query(False, description="Deactivate (soft-delete) this team's apps; their API keys stop working."),
    reassign_children_to: Optional[str] = Query(None, description="Re-parent child teams under this team."),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> TeamDeleteResult:
    """Soft-delete a team. Usage, cost and audit history is never removed.

    Refuses (409) while the team still has apps or child teams unless the
    caller says what should happen to them. The team's registration token is
    always revoked.
    """
    identity.assert_permission("teams:write")
    team = await _get_live_team(db, team_id)
    tid = str(team.id)
    identity.assert_team_access(tid)

    if reassign_to and cascade:
        raise HTTPException(422, "Use either reassign_to or cascade, not both.")

    apps = (await db.execute(
        select(App).where(App.team_id == tid, App.deleted_at.is_(None)).order_by(App.app_id)
    )).scalars().all()
    children = (await db.execute(
        select(Team).where(Team.parent_id == tid, Team.deleted_at.is_(None)).order_by(Team.name)
    )).scalars().all()

    if apps and not reassign_to and not cascade:
        names = ", ".join(a.app_id for a in apps[:5]) + (", ..." if len(apps) > 5 else "")
        raise HTTPException(
            409,
            f"Team '{team.slug}' still has {len(apps)} active app(s) ({names}). "
            "Pass reassign_to=<team_id> to move them, or cascade=true to deactivate them.",
        )
    if children and not reassign_children_to:
        names = ", ".join(c.slug for c in children[:5]) + (", ..." if len(children) > 5 else "")
        raise HTTPException(
            409,
            f"Team '{team.slug}' still has {len(children)} child team(s) ({names}). "
            "Pass reassign_children_to=<team_id> to re-parent them.",
        )

    # Validate every target before mutating anything.
    target: Optional[Team] = None
    if reassign_to:
        target = await _require_live_target(db, identity, reassign_to, param="reassign_to")
        if str(target.id) == tid:
            raise HTTPException(422, "reassign_to: cannot reassign a team's apps to itself.")
        clash = []
        if apps:
            clash = (await db.execute(
                select(App.app_id).where(
                    App.team_id == str(target.id),
                    App.app_id.in_([a.app_id for a in apps]),
                )
            )).scalars().all()
        if clash:
            raise HTTPException(
                409,
                f"Team '{target.slug}' already has app(s) with the same app_id: "
                f"{', '.join(sorted(set(clash)))}. Rename or remove them first.",
            )
    child_target: Optional[Team] = None
    if children:
        child_target = await _require_live_target(
            db, identity, reassign_children_to, param="reassign_children_to")
        if str(child_target.id) == tid:
            raise HTTPException(422, "reassign_children_to: cannot be the team being deleted.")
        if await _is_descendant(db, str(child_target.id), tid):
            raise HTTPException(
                422, "reassign_children_to: target is inside the subtree being deleted; "
                     "choose a team outside it.")

    now = datetime.now(timezone.utc)
    before = _snapshot(team)
    app_ids = [str(a.id) for a in apps]
    n_moved = n_off = 0

    if target is not None and apps:
        new_tid = str(target.id)
        await db.execute(update(App).where(App.id.in_(app_ids)).values(team_id=new_tid))
        await db.execute(update(Threshold).where(Threshold.team_id == tid).values(team_id=new_tid))
        await db.execute(update(GovernancePolicy).where(GovernancePolicy.team_id == tid).values(team_id=new_tid))
        for model in (RealTimeSpend, SessionBudget, AppTopology):
            await db.execute(update(model).where(model.app_id.in_(app_ids)).values(team_id=new_tid))
        for a in apps:
            _audit(db, request, identity, new_tid, "reassigned", resource_type="app",
                   resource_id=str(a.id), before={"team_id": tid, "app_id": a.app_id},
                   after={"team_id": new_tid})
        n_moved = len(apps)
    elif cascade and apps:
        await db.execute(
            update(App).where(App.id.in_(app_ids)).values(is_active=False, deleted_at=now))
        for a in apps:
            _audit(db, request, identity, tid, "deleted", resource_type="app",
                   resource_id=str(a.id), before={"app_id": a.app_id},
                   after={"reason": "team_deleted_cascade"})
        n_off = len(apps)

    child_ids: list[str] = []
    if child_target is not None:
        child_ids = [str(c.id) for c in children]
        for c in children:
            c.parent_id = str(child_target.id)
            _audit(db, request, identity, str(c.id), "updated",
                   before={"parent_id": tid}, after={"parent_id": str(child_target.id)})

    had_token = bool(team.registration_token_hash)
    team.registration_token_hash = None
    team.registration_token_prefix = None
    team.deleted_at = now
    await db.flush()

    _audit(db, request, identity, tid, "deleted", before=before, after={
        "deleted_at": _iso(now),
        "reassign_to": str(target.id) if target else None,
        "apps_reassigned": n_moved,
        "apps_deactivated": n_off,
        "app_ids": app_ids if (n_moved or n_off) else [],
        "reassign_children_to": str(child_target.id) if child_target else None,
        "child_team_ids": child_ids,
        "registration_token_revoked": had_token,
    })

    # Commit before touching the in-process key/app caches so a concurrent
    # request cannot re-populate them from rows that are not yet committed.
    await db.commit()
    if n_moved or n_off:
        from orchestrator.api.ingest import invalidate_key_cache
        for app_uuid in app_ids:
            invalidate_key_cache(app_uuid)

    logger.warning("Team deleted", extra={"team_id": tid, "actor": identity.actor_id})
    return TeamDeleteResult(
        id=tid, slug=team.slug, deleted_at=now,
        apps_reassigned=n_moved, apps_deactivated=n_off,
        child_teams_reassigned=len(child_ids),
        registration_token_revoked=had_token,
    )


# ── Restore ──────────────────────────────────────────────────────────────────

@router.post("/teams/{team_id}/restore", response_model=TeamRestoreResult, tags=["teams"])
async def restore_team(
    team_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> TeamRestoreResult:
    """Undo a soft delete. Apps that were deactivated or moved are NOT
    brought back, and the registration token stays revoked (generate a new one)."""
    identity.assert_permission("teams:write")
    tid = _canon_id(team_id, status_code=404, what="team_id")
    team = await db.get(Team, tid)
    if team is None:
        raise HTTPException(404, "Team not found.")
    identity.assert_team_access(tid)
    if team.deleted_at is None:
        raise HTTPException(409, "Team is not deleted.")

    owner = await _slug_owner(db, team.slug, exclude_id=tid)
    if owner is not None and owner.deleted_at is None:
        raise HTTPException(409, f"Cannot restore: slug '{team.slug}' is now used by another team.")

    before = _snapshot(team)
    parent_cleared = False
    if team.parent_id:
        parent = await db.get(Team, team.parent_id)
        if parent is None or parent.deleted_at is not None:
            team.parent_id = None
            parent_cleared = True
    team.deleted_at = None
    await db.flush()
    _audit(db, request, identity, tid, "restored", before=before,
           after={**_snapshot(team), "parent_cleared": parent_cleared})
    await db.refresh(team)
    detail = await _detail(db, team)
    return TeamRestoreResult(**detail.model_dump(), parent_cleared=parent_cleared)
