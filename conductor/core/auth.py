"""
Modus Conductor — Authentication
========================================
Two auth layers:

1. Orchestrator-to-Conductor auth (push endpoint):
   - Bearer token matching conductor_secret
   - Used when Orchestrators push data

2. Dashboard user auth (read endpoints):
   - Option A (stub): platform-admin for all requests (development)
   - Option B (jwt): Bearer token with RBAC (production)

Same Identity contract as the Orchestrator for dashboard consistency.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

from fastapi import HTTPException, Request

from conductor.core.config import settings

logger = logging.getLogger(__name__)

_VALID_ROLES = {"platform_admin", "team_admin", "team_member", "read_only"}


@dataclass(frozen=True)
class Identity:
    """Dashboard user identity — same contract as Orchestrator."""
    actor_id: str
    role: Literal["platform_admin", "team_admin", "team_member", "read_only"]
    team_ids: list[str] = field(default_factory=list)
    email: Optional[str] = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    user_id: Optional[str] = None

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "platform_admin"

    def assert_team_access(self, team_id: str) -> None:
        if self.is_platform_admin:
            return
        if team_id not in self.team_ids:
            raise HTTPException(status_code=403, detail="Access denied to this team")


def _stub_identity() -> Identity:
    return Identity(
        actor_id="conductor-stub",
        role="platform_admin",
        email=None,
    )


async def get_identity(request: Request) -> Identity:
    """FastAPI dependency for dashboard user identity."""
    if settings.auth_mode == "stub":
        return _stub_identity()

    # JWT mode
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = auth_header[7:]

    try:
        import jwt as pyjwt
        payload = pyjwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256", "RS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except Exception as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid authentication token.")

    role = payload.get("cs_role", "read_only")
    if role not in _VALID_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"Invalid cs_role claim: {role!r}. Must be one of {_VALID_ROLES}.",
        )

    return Identity(
        actor_id=payload.get("sub", "unknown"),
        role=role,
        team_ids=payload.get("cs_teams", []),
        email=payload.get("email"),
    )


async def verify_orchestrator_push(request: Request) -> str:
    """
    FastAPI dependency that verifies an Orchestrator's push credentials.
    Returns the instance_id from the request header.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = auth_header[7:]

    # Verify against conductor_secret (timing-safe comparison)
    if not settings.conductor_secret:
        raise HTTPException(
            status_code=500,
            detail="Conductor secret not configured. Set CONDUCTOR_CONDUCTOR_SECRET.",
        )
    if not hmac.compare_digest(token, settings.conductor_secret):
        raise HTTPException(status_code=403, detail="Invalid conductor secret")

    instance_id = request.headers.get("X-Orchestrator-Instance-ID", "")
    if not instance_id:
        raise HTTPException(
            status_code=400,
            detail="Missing X-Orchestrator-Instance-ID header",
        )

    return instance_id
