"""
Modus — Authentication Middleware
==========================================
Option A stub + Option B JWT + fine-grained RBAC.

Auth modes:
    Option A (stub):  Every request is platform-admin. No login required.
                      Suitable for POC / internal team deployment.
    Option B (jwt):   JWT validation with RBAC permission resolution.
                      Permissions loaded from DB, cached per-user.

Identity object shape is the stable contract across both modes.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


# ── Identity contract — stable across Option A and B ─────────────────────────

@dataclass(frozen=True)
class Identity:
    """
    Resolved caller identity. Injected into every authenticated endpoint.

    actor_id:    Stable identifier for audit logging.
    role:        Coarse role for backward compat. Derived from RBAC in Option B.
    team_ids:    Teams this identity can access. Empty list = all teams.
    email:       User email (from JWT or user record).
    permissions: Fine-grained permission set (e.g. {"apps:read", "billing:write"}).
                 Empty in stub mode (platform_admin bypasses all checks).
    user_id:     Internal user UUID (None in stub mode).
    """
    actor_id: str
    role: Literal["platform_admin", "team_admin", "team_member", "read_only"]
    team_ids: list[str] = field(default_factory=list)
    email: Optional[str] = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    user_id: Optional[str] = None

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "platform_admin"

    @property
    def team_id(self) -> Optional[str]:
        """Backward-compat: return the primary (first) team id or None.

        Many handlers were written when Identity carried a single
        ``team_id``. The model is now multi-team via ``team_ids``, but
        existing call sites that read ``identity.team_id`` continue to
        work via this property — they receive the first team in the
        list. New code should iterate ``team_ids`` or filter with
        ``.in_(identity.team_ids)`` to handle the multi-team case.
        """
        return self.team_ids[0] if self.team_ids else None

    @property
    def can_write(self) -> bool:
        """Backward-compat: True if identity has any write permission."""
        if self.is_platform_admin:
            return True
        return any(p.endswith(":write") or p.endswith(":delete")
                   or p == "platform:manage"
                   for p in self.permissions)

    def has_permission(self, permission: str) -> bool:
        """Check fine-grained permission. Platform admin has all permissions."""
        if self.is_platform_admin:
            return True
        return permission in self.permissions

    def has_any_permission(self, *permissions: str) -> bool:
        if self.is_platform_admin:
            return True
        return any(p in self.permissions for p in permissions)

    def assert_permission(self, permission: str) -> None:
        """Raise 403 if this identity lacks the given permission."""
        if not self.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' required.",
            )

    def assert_any_permission(self, *permissions: str) -> None:
        """Raise 403 if this identity lacks all of the given permissions."""
        if not self.has_any_permission(*permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of {list(permissions)} required.",
            )

    def can_access_team(self, team_id: str) -> bool:
        """True if this identity can see data for the given team."""
        if self.is_platform_admin:
            return True
        return team_id in self.team_ids

    def assert_team_access(self, team_id: str) -> None:
        """Raise 403 if this identity cannot access the team."""
        if not self.can_access_team(team_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to team {team_id!r} is not permitted.",
            )

    def assert_write_access(self) -> None:
        """Raise 403 if this identity is read-only."""
        if not self.can_write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Write access required.",
            )


# ── Master-key service identity ──────────────────────────────────────────────
#
# The master key (MODUS_MASTER_API_KEY, ``mds_master_...``) is a root
# credential for machine callers: the policy CLI, the GitHub Action, the seed
# script and the dashboard "master key" sign-in. It is presented in the
# ``X-Modus-APIKey`` header and maps to a platform-admin service identity in
# BOTH auth modes. Treat it like a root password.

MASTER_KEY_HEADER = "X-Modus-APIKey"
MASTER_KEY_ACTOR_ID = "master-key"

_MASTER_IDENTITY = Identity(
    actor_id=MASTER_KEY_ACTOR_ID,
    role="platform_admin",
    team_ids=[],
    email=None,
)


def verify_master_key(provided: str) -> bool:
    """Constant-time check of a presented value against the master key."""
    expected = settings.master_api_key or ""
    if not provided or not expected:
        return False
    # compare_digest on bytes: str inputs raise TypeError on non-ASCII.
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# ── Option A: stub resolver ───────────────────────────────────────────────────

_STUB_IDENTITY = Identity(
    actor_id="platform-admin",
    role="platform_admin",
    team_ids=[],  # empty = no restriction
    email=None,
)


def _resolve_stub(request: Request) -> Identity:
    """
    Passthrough. Returns platform-admin identity for every request.
    No token required. Protected by network perimeter only.
    """
    # Defense-in-depth behind the config validator: stub auth must never
    # resolve to a platform-admin identity outside development. The config
    # validator already refuses to start with stub auth in staging/production
    # (unless MODUS_ALLOW_INSECURE_AUTH=1), but if that gate is ever bypassed
    # we fail closed here rather than silently handing out admin access.
    if settings.environment != "development":
        import os

        if os.environ.get("MODUS_ALLOW_INSECURE_AUTH") != "1":
            logger.critical(
                "SECURITY: Stub auth mode reached in environment=%r without "
                "MODUS_ALLOW_INSECURE_AUTH=1. Refusing to grant unauthenticated "
                "platform-admin access. Set MODUS_AUTH_MODE=jwt and configure "
                "JWT secrets.",
                settings.environment,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication is not configured for this environment.",
            )
    return _STUB_IDENTITY


# ── Option B: JWT resolver ────────────────────────────────────────────────────

_VALID_ROLES = {"platform_admin", "team_admin", "team_member", "read_only"}


def _resolve_jwt(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Identity:
    """
    Validate a Bearer JWT and return an Identity.

    Expected JWT claims:
        sub:        actor_id (user or service account ID)  — required
        exp:        expiration timestamp                    — required
        email:      user email                              — optional
        cs_role:    "platform_admin" | "team_admin" | "team_member" | "read_only"
        cs_teams:   list of team slugs this identity can access
                    (omit or empty list for platform_admin)

    Algorithm auto-detection:
        If MODUS_JWT_SECRET starts with "-----BEGIN" → RS256 (PEM key)
        Otherwise → HS256 (shared secret)
    """
    import jwt as pyjwt

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    key = settings.jwt_secret
    if not key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SSO auth mode requires the authentication secret to be configured.",
        )

    algorithms = ["RS256"] if key.startswith("-----BEGIN") else ["HS256"]

    try:
        payload = pyjwt.decode(
            credentials.credentials,
            key,
            algorithms=algorithms,
            issuer=settings.jwt_issuer or None,
            audience=settings.jwt_audience or None,
            options={
                "require": ["sub", "exp"],
                "verify_exp": True,
                "verify_iss": bool(settings.jwt_issuer),
                "verify_aud": bool(settings.jwt_audience),
            },
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except pyjwt.InvalidIssuerError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed — issuer mismatch.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except pyjwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed — audience mismatch.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except pyjwt.InvalidTokenError as exc:
        logger.warning("Token validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role = payload.get("cs_role", "read_only")
    if role not in _VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Invalid cs_role claim: {role!r}. Must be one of {_VALID_ROLES}.",
        )

    team_ids = list(payload.get("cs_teams") or [])

    # Base identity from JWT claims (before RBAC enrichment)
    base_identity = Identity(
        actor_id=payload.get("sub", ""),
        role=role,
        team_ids=team_ids if isinstance(team_ids, list) else [],
        email=payload.get("email"),
    )

    return base_identity


async def _enrich_with_rbac(
    base_identity: Identity,
    db: "AsyncSession",
) -> Identity:
    """
    Enrich a JWT-derived identity with RBAC permissions from the database.
    Looks up the user by external_id (JWT sub), resolves permissions,
    and returns an enriched Identity with the full permission set.

    If the user doesn't exist in the DB yet (first login), falls back
    to the coarse role from the JWT claims.
    """
    from orchestrator.core.permissions import (
        get_user_by_external_id,
        resolve_permissions,
    )

    user = await get_user_by_external_id(base_identity.actor_id, db)
    if user is None:
        # User not yet provisioned — use JWT claims only
        return base_identity

    resolved = await resolve_permissions(str(user.id), db)

    # Determine coarse role from resolved permissions for backward compat
    if resolved.is_platform_admin:
        coarse_role = "platform_admin"
    elif resolved.has("teams:members") or resolved.has("teams:write"):
        coarse_role = "team_admin"
    elif any(p.endswith(":write") for p in resolved.permissions):
        coarse_role = "team_member"
    else:
        coarse_role = "read_only"

    return Identity(
        actor_id=base_identity.actor_id,
        role=coarse_role,
        team_ids=list(resolved.team_ids),
        email=base_identity.email or user.email,
        permissions=resolved.permissions,
        user_id=str(user.id),
    )


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_identity(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Identity:
    """
    FastAPI dependency. Resolves caller identity based on auth_mode setting.

    In JWT mode, also enriches the identity with RBAC permissions from the DB.

    Usage:
        @router.get("/resource")
        async def handler(identity: Identity = Depends(get_identity)):
            identity.assert_permission("apps:read")
            ...
    """
    presented_key = request.headers.get(MASTER_KEY_HEADER, "").strip()
    if presented_key and verify_master_key(presented_key):
        return _MASTER_IDENTITY

    if settings.auth_mode == "stub":
        # Stub mode is open by design; a non-master X-Modus-APIKey (e.g. an
        # app key sent by an SDK) does not change that.
        return _resolve_stub(request)
    elif settings.auth_mode == "jwt":
        if presented_key and not credentials:
            # A key was presented but it is not the master key, and there is
            # no bearer JWT to fall back on.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid master key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        base = _resolve_jwt(request, credentials)
        # Enrich with DB-backed RBAC permissions
        from orchestrator.db.session import get_session_ctx
        async with get_session_ctx() as db:
            return await _enrich_with_rbac(base, db)
    else:
        raise RuntimeError(f"Unknown auth_mode: {settings.auth_mode!r}")


# ── Agent key resolver — separate from user identity ──────────────────────────

async def get_app_identity(request: Request) -> str:
    """
    Extract the agent credential from an SDK request (ingest, policy sync,
    routing outcomes).

    This is separate from get_identity() because agent requests are not
    user-initiated and do not carry user JWTs.

    Accepts both credential forms an agent can hold: a stable per-app key
    (``mds_`` prefix, issued by app registration) and a session token
    (``mst_`` prefix, issued by team-token self-registration).

    This only checks the shape of the credential and returns the raw key. It
    does NOT authenticate: every route using this dependency must verify the
    key with ``orchestrator.api.ingest._verify_app_key`` before trusting it.
    """
    from orchestrator.core.session_token import SESSION_TOKEN_PREFIX

    key = (
        request.headers.get("X-Modus-APIKey")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    if not key or not key.startswith((settings.api_key_prefix, SESSION_TOKEN_PREFIX)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid X-Modus-APIKey header required.",
        )
    return key
