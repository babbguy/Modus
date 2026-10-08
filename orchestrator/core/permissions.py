"""
Modus — Permission Resolver
=====================================
Resolves a user's effective permissions from RBAC assignments,
team hierarchy, and allow/deny rules.

Resolution algorithm:
  1. Load all RbacAssignments for the user
  2. For each assignment, load the RbacRole (allow + deny lists)
  3. For team-scoped assignments, expand the team hierarchy downward
  4. Union all 'allow' permissions → allowed set
  5. Union all 'deny' permissions → denied set
  6. Effective permissions = allowed - denied
  7. Deny always wins

Wildcard handling:
  - "*" in allow → all permissions granted (except explicit denies)
  - "apps:*" → all actions on apps resource
  - "deny: ['*']" at any level → fully locked out of that scope

Caching:
  - Resolved permissions are cached per-user for the configured TTL.
  - Cache is invalidated on role/assignment changes via clear_permissions_cache().
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from orchestrator.db.models import (
    RbacAssignment,
    Team,
    TeamMembership,
    User,
)

logger = logging.getLogger(__name__)

# All defined permissions in the system — used for wildcard expansion
ALL_PERMISSIONS: set[str] = {
    # Platform administration
    "platform:manage",
    "users:read", "users:invite", "users:write", "users:delete",
    "roles:read", "roles:write", "roles:delete",
    "audit:read", "audit:write",
    "teams:read", "teams:write", "teams:delete", "teams:members",
    # Application management
    "apps:read", "apps:write", "apps:delete", "apps:keys",
    # Cost & billing
    "billing:read", "billing:write", "billing:export",
    "pricing:read", "pricing:write",
    # Operations & monitoring
    "thresholds:read", "thresholds:write", "thresholds:delete",
    "alerts:read", "alerts:write",
    "notifications:read", "notifications:write", "notifications:delete",
    # Intelligence & insights
    "insights:read", "insights:write",
    "reports:read", "reports:write",
    # Policy & routing
    "policies:read", "policies:write", "policies:delete",
    "routing:read", "routing:write",
    # Topology
    "topology:read", "topology:write",
    # Dashboard views
    "view:overview", "view:devops", "view:executive", "view:billing", "view:admin",
}

# Cache: user_id → (ResolvedPermissions, expiry_timestamp)
_cache: dict[str, tuple["ResolvedPermissions", float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass(frozen=True)
class ResolvedPermissions:
    """Effective permissions for a user after allow/deny resolution."""
    permissions: frozenset[str]
    team_ids: frozenset[str]
    is_platform_admin: bool

    def has(self, permission: str) -> bool:
        """Check if the user has a specific permission."""
        if self.is_platform_admin and permission not in self._denied:
            return True
        return permission in self.permissions

    def has_any(self, *permissions: str) -> bool:
        return any(self.has(p) for p in permissions)

    def has_all(self, *permissions: str) -> bool:
        return all(self.has(p) for p in permissions)

    def can_access_team(self, team_id: str) -> bool:
        if self.is_platform_admin:
            return True
        return team_id in self.team_ids

    # Internal: track denies for platform_admin wildcard check
    _denied: frozenset[str] = field(default=frozenset())


def _expand_wildcards(perms: list[str]) -> set[str]:
    """Expand wildcard permissions like '*' and 'apps:*' into concrete sets."""
    result: set[str] = set()
    for p in perms:
        if p == "*":
            result.update(ALL_PERMISSIONS)
        elif p.endswith(":*"):
            prefix = p[:-1]  # "apps:"
            result.update(perm for perm in ALL_PERMISSIONS if perm.startswith(prefix))
        else:
            result.add(p)
    return result


async def _get_team_subtree_ids(team_id: str, db: AsyncSession) -> set[str]:
    """
    Get all team IDs in the subtree rooted at team_id (inclusive).
    Uses iterative BFS to avoid recursion limits on deep hierarchies.
    """
    result = {team_id}
    frontier = [team_id]

    while frontier:
        rows = (await db.execute(
            select(Team.id).where(
                Team.parent_id.in_(frontier),
                Team.deleted_at.is_(None),
            )
        )).scalars().all()
        child_ids = [str(r) for r in rows]
        new_ids = [cid for cid in child_ids if cid not in result]
        result.update(new_ids)
        frontier = new_ids

    return result


async def resolve_permissions(
    user_id: str,
    db: AsyncSession,
    *,
    bypass_cache: bool = False,
) -> ResolvedPermissions:
    """
    Resolve the effective permissions for a user.

    Steps:
      1. Check cache
      2. Load all RBAC assignments for the user (with roles eager-loaded)
      3. For each assignment, expand allow/deny with wildcards
      4. For team-scoped assignments, expand team hierarchy
      5. Compute effective = allow - deny
      6. Determine team visibility from memberships + assignments
    """
    # Check cache
    if not bypass_cache and user_id in _cache:
        cached, expiry = _cache[user_id]
        if time.monotonic() < expiry:
            return cached

    # Load assignments with roles
    assignments = (await db.execute(
        select(RbacAssignment)
        .where(RbacAssignment.user_id == user_id)
        .options(selectinload(RbacAssignment.role))
    )).scalars().all()

    all_allow: set[str] = set()
    all_deny: set[str] = set()
    team_ids: set[str] = set()
    is_platform_admin = False

    for assignment in assignments:
        role = assignment.role
        if role is None:
            continue

        allow = _expand_wildcards(role.allow or [])
        deny = _expand_wildcards(role.deny or [])

        if assignment.team_id is None:
            # Platform-level assignment — applies globally
            all_allow.update(allow)
            all_deny.update(deny)
            if "*" in (role.allow or []):
                is_platform_admin = True
        else:
            # Team-scoped — permissions apply within team subtree
            all_allow.update(allow)
            all_deny.update(deny)
            subtree = await _get_team_subtree_ids(assignment.team_id, db)
            team_ids.update(subtree)

    # Also add teams from direct memberships
    memberships = (await db.execute(
        select(TeamMembership.team_id).where(TeamMembership.user_id == user_id)
    )).scalars().all()
    for tid in memberships:
        team_ids.add(str(tid))

    # Platform admin sees all teams
    if is_platform_admin:
        team_ids = frozenset()  # empty = all teams (convention from existing code)

    # Effective = allow - deny
    effective = all_allow - all_deny

    result = ResolvedPermissions(
        permissions=frozenset(effective),
        team_ids=frozenset(team_ids) if not is_platform_admin else frozenset(),
        is_platform_admin=is_platform_admin,
        _denied=frozenset(all_deny),
    )

    # Cache
    _cache[user_id] = (result, time.monotonic() + _CACHE_TTL_SECONDS)

    return result


def clear_permissions_cache(user_id: Optional[str] = None) -> None:
    """
    Invalidate cached permissions.
    If user_id is given, clears only that user's cache.
    If None, clears the entire cache.
    """
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)


async def get_user_by_external_id(
    external_id: str, db: AsyncSession
) -> Optional[User]:
    """Look up a user by their SSO/JWT external_id (sub claim)."""
    return (await db.execute(
        select(User).where(User.external_id == external_id, User.is_active.is_(True))
    )).scalar_one_or_none()


async def get_user_by_email(
    email: str, db: AsyncSession
) -> Optional[User]:
    """Look up a user by email."""
    return (await db.execute(
        select(User).where(User.email == email, User.is_active.is_(True))
    )).scalar_one_or_none()
