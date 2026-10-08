"""
Modus — Roles Router
===========================
RBAC role management and user-role assignments.

Endpoints:
    GET    /api/v1/roles                           — List roles
    POST   /api/v1/roles                           — Create custom role
    GET    /api/v1/roles/{role_id}                  — Get role detail
    PATCH  /api/v1/roles/{role_id}                  — Update role allow/deny
    DELETE /api/v1/roles/{role_id}                  — Delete custom role
    POST   /api/v1/roles/{role_id}/clone            — Clone role as custom

    GET    /api/v1/roles/assignments                — List assignments
    POST   /api/v1/roles/assignments                — Assign role to user
    DELETE /api/v1/roles/assignments/{id}           — Revoke assignment

    GET    /api/v1/roles/permissions                 — List all available permissions
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.permissions import ALL_PERMISSIONS, clear_permissions_cache
from orchestrator.db.models import (
    AuditLog,
    RbacAssignment,
    RbacRole,
    Team,
    User,
)
from orchestrator.db.session import get_session

router = APIRouter()


# ── Response / Request models ─────────────────────────────────────────────────

class RoleResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    allow: list[str]
    deny: list[str]
    is_system: bool
    created_at: datetime


class RoleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    description: Optional[str] = None
    allow: list[str] = Field(..., min_length=1)
    deny: list[str] = Field(default_factory=list)


class RoleUpdateRequest(BaseModel):
    description: Optional[str] = None
    allow: Optional[list[str]] = None
    deny: Optional[list[str]] = None


class RoleCloneRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    description: Optional[str] = None


class AssignmentResponse(BaseModel):
    id: str
    user_id: str
    user_email: Optional[str] = None
    user_display_name: Optional[str] = None
    role_id: str
    role_name: str
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    assigned_by: Optional[str] = None
    created_at: datetime


class AssignmentCreateRequest(BaseModel):
    user_id: str
    role_id: str
    team_id: Optional[str] = None


class PermissionInfo(BaseModel):
    permission: str
    resource: str
    action: str


# ── Role CRUD ─────────────────────────────────────────────────────────────────

@router.get("/roles", response_model=list[RoleResponse])
async def list_roles(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[RoleResponse]:
    identity.assert_permission("roles:read")
    rows = (await db.execute(
        select(RbacRole).order_by(RbacRole.name)
    )).scalars().all()
    return [RoleResponse(
        id=str(r.id), name=r.name, description=r.description,
        allow=r.allow or [], deny=r.deny or [],
        is_system=r.is_system, created_at=r.created_at,
    ) for r in rows]


@router.post("/roles", response_model=RoleResponse, status_code=201)
async def create_role(
    body: RoleCreateRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> RoleResponse:
    identity.assert_permission("roles:write")

    # Validate permission strings
    _validate_permissions(body.allow + body.deny)

    existing = (await db.execute(
        select(RbacRole).where(RbacRole.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Role '{body.name}' already exists.")

    role = RbacRole(
        name=body.name,
        description=body.description,
        allow=body.allow,
        deny=body.deny,
        is_system=False,
    )
    db.add(role)
    await db.flush()

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="rbac_role", resource_id=str(role.id),
        action="created", after=body.model_dump(),
    ))

    return RoleResponse(
        id=str(role.id), name=role.name, description=role.description,
        allow=role.allow, deny=role.deny,
        is_system=role.is_system, created_at=role.created_at,
    )


# NOTE: the static GET paths /roles/assignments and /roles/permissions must be
# declared BEFORE /roles/{role_id}. Starlette matches routes in declaration
# order, and a path-param validation failure on the parameterized route returns
# 422 instead of falling through to later routes.

@router.get("/roles/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    user_id: Optional[str] = Query(None),
    team_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[AssignmentResponse]:
    identity.assert_permission("roles:read")

    q = select(RbacAssignment).options(selectinload(RbacAssignment.role))
    if user_id:
        q = q.where(RbacAssignment.user_id == user_id)
    if team_id:
        q = q.where(RbacAssignment.team_id == team_id)
    q = q.order_by(RbacAssignment.created_at.desc()).limit(limit).offset(offset)

    rows = (await db.execute(q)).scalars().all()
    results = []
    for a in rows:
        # Look up user info
        user_email = None
        user_display = None
        user = (await db.execute(
            select(User).where(User.id == a.user_id)
        )).scalar_one_or_none()
        if user:
            user_email = user.email
            user_display = user.display_name

        # Look up team name
        team_name = None
        if a.team_id:
            team = (await db.execute(
                select(Team).where(Team.id == a.team_id)
            )).scalar_one_or_none()
            if team:
                team_name = team.name

        results.append(AssignmentResponse(
            id=str(a.id), user_id=a.user_id,
            user_email=user_email, user_display_name=user_display,
            role_id=str(a.role_id), role_name=a.role.name if a.role else "unknown",
            team_id=str(a.team_id) if a.team_id else None,
            team_name=team_name,
            assigned_by=a.assigned_by, created_at=a.created_at,
        ))
    return results


@router.get("/roles/permissions", response_model=list[PermissionInfo])
async def list_permissions(
    identity: Identity = Depends(get_identity),
) -> list[PermissionInfo]:
    identity.assert_permission("roles:read")
    result = []
    for perm in sorted(ALL_PERMISSIONS):
        resource, action = perm.split(":", 1)
        result.append(PermissionInfo(permission=perm, resource=resource, action=action))
    return result


@router.get("/roles/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> RoleResponse:
    identity.assert_permission("roles:read")
    role = (await db.execute(
        select(RbacRole).where(RbacRole.id == role_id)
    )).scalar_one_or_none()
    if not role:
        raise HTTPException(404, "Role not found.")
    return RoleResponse(
        id=str(role.id), name=role.name, description=role.description,
        allow=role.allow or [], deny=role.deny or [],
        is_system=role.is_system, created_at=role.created_at,
    )


@router.patch("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    request: Request,
    body: RoleUpdateRequest,
    role_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> RoleResponse:
    identity.assert_permission("roles:write")
    role = (await db.execute(
        select(RbacRole).where(RbacRole.id == role_id)
    )).scalar_one_or_none()
    if not role:
        raise HTTPException(404, "Role not found.")

    before = {"allow": role.allow, "deny": role.deny, "description": role.description}

    if body.allow is not None:
        _validate_permissions(body.allow)
        role.allow = body.allow
    if body.deny is not None:
        _validate_permissions(body.deny)
        role.deny = body.deny
    if body.description is not None:
        role.description = body.description

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="rbac_role", resource_id=role_id,
        action="updated", before=before,
        after=body.model_dump(exclude_none=True),
    ))

    # Invalidate all permission caches — role change affects all assigned users
    clear_permissions_cache()

    return RoleResponse(
        id=str(role.id), name=role.name, description=role.description,
        allow=role.allow or [], deny=role.deny or [],
        is_system=role.is_system, created_at=role.created_at,
    )


@router.delete("/roles/{role_id}", status_code=204, response_model=None)
async def delete_role(
    request: Request,
    role_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    identity.assert_permission("roles:delete")
    role = (await db.execute(
        select(RbacRole).where(RbacRole.id == role_id)
    )).scalar_one_or_none()
    if not role:
        raise HTTPException(404, "Role not found.")
    if role.is_system:
        raise HTTPException(400, f"Cannot delete system role '{role.name}'.")

    # Check for active assignments
    assignment_count = (await db.execute(
        select(RbacAssignment).where(RbacAssignment.role_id == role_id)
    )).scalars().all()
    if assignment_count:
        raise HTTPException(
            400,
            f"Cannot delete role with {len(assignment_count)} active assignment(s). "
            "Reassign users first.",
        )

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="rbac_role", resource_id=role_id,
        action="deleted", before={"name": role.name},
    ))
    await db.delete(role)


@router.post("/roles/{role_id}/clone", response_model=RoleResponse, status_code=201)
async def clone_role(
    request: Request,
    body: RoleCloneRequest,
    role_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> RoleResponse:
    identity.assert_permission("roles:write")

    source = (await db.execute(
        select(RbacRole).where(RbacRole.id == role_id)
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(404, "Source role not found.")

    existing = (await db.execute(
        select(RbacRole).where(RbacRole.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Role '{body.name}' already exists.")

    clone = RbacRole(
        name=body.name,
        description=body.description or f"Clone of {source.name}",
        allow=list(source.allow or []),
        deny=list(source.deny or []),
        is_system=False,
    )
    db.add(clone)
    await db.flush()

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="rbac_role", resource_id=str(clone.id),
        action="cloned",
        before={"source_role_id": role_id, "source_name": source.name},
        after={"name": clone.name},
    ))

    return RoleResponse(
        id=str(clone.id), name=clone.name, description=clone.description,
        allow=clone.allow or [], deny=clone.deny or [],
        is_system=clone.is_system, created_at=clone.created_at,
    )


# ── Role Assignments ──────────────────────────────────────────────────────────
# (GET /roles/assignments is declared above /roles/{role_id} — see NOTE there.)

@router.post("/roles/assignments", response_model=AssignmentResponse, status_code=201)
async def assign_role(
    body: AssignmentCreateRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> AssignmentResponse:
    identity.assert_permission("roles:write")

    # Validate user exists
    user = (await db.execute(
        select(User).where(User.id == body.user_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found.")

    # Validate role exists
    role = (await db.execute(
        select(RbacRole).where(RbacRole.id == body.role_id)
    )).scalar_one_or_none()
    if not role:
        raise HTTPException(404, "Role not found.")

    # Validate team if specified
    team_name = None
    if body.team_id:
        team = (await db.execute(
            select(Team).where(Team.id == body.team_id, Team.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not team:
            raise HTTPException(404, "Team not found.")
        team_name = team.name

    # Check for existing assignment
    existing = (await db.execute(
        select(RbacAssignment).where(
            RbacAssignment.user_id == body.user_id,
            RbacAssignment.role_id == body.role_id,
            RbacAssignment.team_id == body.team_id if body.team_id
            else RbacAssignment.team_id.is_(None),
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "This role assignment already exists.")

    assignment = RbacAssignment(
        user_id=body.user_id,
        role_id=body.role_id,
        team_id=body.team_id,
        assigned_by=identity.actor_id,
    )
    db.add(assignment)
    await db.flush()

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="rbac_assignment", resource_id=str(assignment.id),
        action="created",
        after={"user_id": body.user_id, "role": role.name,
               "team_id": body.team_id},
    ))

    clear_permissions_cache(body.user_id)

    return AssignmentResponse(
        id=str(assignment.id), user_id=body.user_id,
        user_email=user.email, user_display_name=user.display_name,
        role_id=str(role.id), role_name=role.name,
        team_id=body.team_id, team_name=team_name,
        assigned_by=identity.actor_id, created_at=assignment.created_at,
    )


@router.delete("/roles/assignments/{assignment_id}", status_code=204, response_model=None)
async def revoke_assignment(
    request: Request,
    assignment_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    identity.assert_permission("roles:write")
    assignment = (await db.execute(
        select(RbacAssignment).where(RbacAssignment.id == assignment_id)
    )).scalar_one_or_none()
    if not assignment:
        raise HTTPException(404, "Assignment not found.")

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="rbac_assignment", resource_id=assignment_id,
        action="revoked",
        before={"user_id": assignment.user_id, "role_id": str(assignment.role_id),
                "team_id": str(assignment.team_id) if assignment.team_id else None},
    ))

    user_id = assignment.user_id
    await db.delete(assignment)
    clear_permissions_cache(user_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_permissions(perms: list[str]) -> None:
    """Validate that all permissions are known or are valid wildcards."""
    for p in perms:
        if p == "*":
            continue
        if p.endswith(":*"):
            resource = p[:-2]
            if not any(perm.startswith(f"{resource}:") for perm in ALL_PERMISSIONS):
                raise HTTPException(
                    400, f"Unknown resource in permission wildcard: {p!r}"
                )
            continue
        if p not in ALL_PERMISSIONS:
            raise HTTPException(400, f"Unknown permission: {p!r}")
