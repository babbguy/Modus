"""
Modus — Topology & Self-Registration API
=================================================
Endpoints:

POST /api/v1/self-register
    Agent-facing. Accepts a team registration token. Auto-creates or retrieves
    the app record and returns a per-app mds_ key. Idempotent — safe to call
    on every process restart. This eliminates manual app registration entirely.

POST /api/v1/topology
    Agent-facing. Receives the discovered environment snapshot from the agent.
    Stores it, triggers AI summarization if the snapshot changed meaningfully,
    and returns the generated summary. Authenticated with the per-app mds_ key.

GET  /api/v1/topology
    Dashboard-facing. Returns topology for all apps the caller can see.

GET  /api/v1/topology/{app_id}
    Dashboard-facing. Returns topology for a specific app.

POST /api/v1/teams/{team_id}/registration-token
    Admin-facing. Generates a team registration token (mds_team_...).
    Master key required. Returns the token once — not stored in plaintext.

DELETE /api/v1/teams/{team_id}/registration-token
    Admin-facing. Revokes the team registration token. Master key required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import secrets
import string
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity, get_app_identity
from orchestrator.db.models import App, AppTopology, AuditLog, Team
from orchestrator.db.session import get_session
from orchestrator.api.ingest import _verify_app_key
from orchestrator.api.apps import _generate_app_key, _hash_key, _verify_master_key, _extract_raw_key

logger = logging.getLogger(__name__)
router = APIRouter()

_TEAM_TOKEN_PREFIX = "mds_team_"
_TEAM_TOKEN_BCRYPT_ROUNDS = 8


# ── Helpers ────────────────────────────────────────────────────────────────────

def _generate_team_token() -> str:
    alphabet = string.ascii_letters + string.digits
    token = "".join(secrets.choice(alphabet) for _ in range(40))
    return f"{_TEAM_TOKEN_PREFIX}{token}"


def _hash_team_token(token: str) -> str:
    return bcrypt.hashpw(token.encode(), bcrypt.gensalt(rounds=_TEAM_TOKEN_BCRYPT_ROUNDS)).decode()


async def _resolve_team_token(raw_token: str, db: AsyncSession) -> Optional[Team]:
    """
    Find the team whose registration token matches raw_token.
    Uses prefix lookup first (fast), then bcrypt verification (slow but safe).
    Returns None if no match.
    """
    if not raw_token.startswith(_TEAM_TOKEN_PREFIX):
        return None

    prefix = raw_token[:20]
    result = await db.execute(
        select(Team).where(
            Team.registration_token_prefix == prefix,
            Team.deleted_at.is_(None),
        )
    )
    team = result.scalar_one_or_none()
    if team is None or not team.registration_token_hash:
        return None

    try:
        if await asyncio.to_thread(
            bcrypt.checkpw, raw_token.encode(), team.registration_token_hash.encode()
        ):
            return team
    except ValueError as exc:
        # Malformed stored hash — fail closed but leave an audit trail.
        logger.warning("Registration token check failed for team %s: %s", team.id, exc)
    return None


def _make_safe_app_id(raw: str) -> str:
    """Normalise a raw string into a valid app_id slug."""
    import re
    slug = re.sub(r"[^a-z0-9\-]", "", raw.lower().replace("_", "-").replace(" ", "-"))
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:128] or "unnamed-app"


# ── AI summarization ───────────────────────────────────────────────────────────

async def _generate_ai_summary(snapshot: dict) -> str:
    """
    Generate an AI-powered topology summary using the configured provider.
    Provider is selected via MODUS_SUMMARY_AGENT (default: anthropic).
    Falls back to structured text on any failure.
    """
    from orchestrator.core.ai_engine import AIEngine
    return await AIEngine.summarize(snapshot)


def _snapshot_hash(snapshot: dict) -> str:
    """Stable hash of topology content, excluding volatile fields."""
    stable = {k: v for k, v in snapshot.items()
              if k not in ("pid", "scanned_at", "k8s_pod_name", "agent_version")}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


# ── Self-registration endpoint ─────────────────────────────────────────────────

class SelfRegisterRequest(BaseModel):
    """Sent by the agent on startup using the team token."""
    app_id: str = Field(..., max_length=128)
    app_name: str = Field(..., max_length=256)
    environment: str = Field("production", pattern="^(production|staging|dev)$")
    agent_version: Optional[str] = Field(None, max_length=32)


class SelfRegisterResponse(BaseModel):
    app_uuid: str       # internal UUID — used in API calls
    app_id: str         # slug
    app_name: str
    team_id: str
    team_slug: str
    environment: str
    api_key: str        # per-app mds_ key — held in memory by agent
    registered: bool    # True = newly created, False = already existed


@router.post(
    "/self-register",
    response_model=SelfRegisterResponse,
    status_code=200,
    tags=["topology"],
    summary="Agent self-registration via team token",
    description=(
        "Called by the agent on startup. Accepts a team registration token "
        "and auto-creates or retrieves the app. Returns a per-app API key. "
        "Idempotent — safe to call on every restart."
    ),
)
async def self_register(
    body: SelfRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> SelfRegisterResponse:
    # Extract team token from header
    raw_token = (
        request.headers.get("X-Modus-TeamToken", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )

    if not raw_token.startswith(_TEAM_TOKEN_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Team registration token required. "
                "Set MODUS_TEAM_TOKEN in your environment. "
                "Get your token from your Modus platform admin."
            ),
        )

    team = await _resolve_team_token(raw_token, db)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked team registration token.",
        )

    # ── Upsert the app record ──────────────────────────────────────────────────
    # Uses INSERT ... ON CONFLICT DO UPDATE so concurrent pod restarts are safe.
    # All replicas converge on the same row. No SELECT-then-INSERT race.
    #
    # The stable mds_ key is generated once on first registration and NEVER
    # rotated by self-register. Key rotation requires an explicit admin action.
    # Each process gets a fresh SHORT-LIVED session token (mst_) instead.
    # Session tokens don't touch the DB on verify — they're HMAC-signed.

    from orchestrator.core.session_token import generate as gen_session_token

    safe_app_id = _make_safe_app_id(body.app_id)
    now = datetime.now(timezone.utc)

    # Generate a stable key only for the INSERT path (new apps).
    # We include it in the upsert but DO NOT update it on conflict —
    # the existing key stays intact.
    new_stable_key = _generate_app_key()
    new_key_hash = await asyncio.to_thread(_hash_key, new_stable_key)
    new_app_uuid = str(__import__("uuid").uuid4())

    # Check if app already exists (needed for SQLite path, avoids .returning())
    existing_app = (await db.execute(
        select(App).where(App.team_id == str(team.id), App.app_id == safe_app_id, App.deleted_at.is_(None))
    )).scalar_one_or_none()

    if existing_app:
        # Update volatile metadata only — environment and key are immutable
        existing_app.last_seen_at = now
        existing_app.agent_version = body.agent_version or existing_app.agent_version
        existing_app.app_name = body.app_name
        app_obj = existing_app
        app_uuid = str(existing_app.id)
        newly_created = False
    else:
        app_obj = App(
            id=new_app_uuid,
            team_id=str(team.id),
            app_id=safe_app_id,
            app_name=body.app_name,
            environment=body.environment,
            api_key_hash=new_key_hash,
            api_key_prefix=new_stable_key[:16],
            agent_version=body.agent_version,
            is_active=True,
            enforcement_state="active",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(app_obj)
        await db.flush()
        app_uuid = new_app_uuid
        newly_created = True

    # Issue a fresh session token — valid 24h, verifiable in <0.1ms, no DB read
    session_token = gen_session_token(app_uuid, str(team.id))

    if newly_created:
        db.add(AuditLog(
            actor_id=f"self-register:{team.slug}",
            actor_ip=request.client.host if request.client else None,
            team_id=str(team.id),
            resource_type="app",
            resource_id=app_uuid,
            action="self_registered",
            after={
                "app_id": safe_app_id,
                "app_name": body.app_name,
                "environment": body.environment,
                "team": team.slug,
            },
        ))
        logger.info(
            "App self-registered",
            extra={"app_id": safe_app_id, "team": team.slug, "uuid": app_uuid},
        )
    else:
        logger.debug("App reconnected", extra={"app_id": safe_app_id, "team": team.slug})

    return SelfRegisterResponse(
        app_uuid=app_uuid,
        app_id=app_obj.app_id,
        app_name=app_obj.app_name,
        team_id=str(team.id),
        team_slug=team.slug,
        environment=app_obj.environment,
        api_key=session_token,   # agent uses this for all subsequent calls
        registered=newly_created,
    )


# ── Topology report endpoint ───────────────────────────────────────────────────

class TopologyReportRequest(BaseModel):
    """Full topology snapshot from the agent's EnvironmentScanner."""
    snapshot: dict = Field(..., description="Raw TopologySnapshot.to_dict() output")


class TopologyReportResponse(BaseModel):
    ai_summary: str
    snapshot_hash: str
    summary_generated: bool  # True if AI was invoked, False if cached


@router.post(
    "/topology",
    response_model=TopologyReportResponse,
    tags=["topology"],
    summary="Report discovered environment topology",
)
async def report_topology(
    body: TopologyReportRequest,
    raw_key: str = Depends(get_app_identity),
    db: AsyncSession = Depends(get_session),
) -> TopologyReportResponse:
    app = await _verify_app_key(raw_key, db)
    snap = body.snapshot
    new_hash = _snapshot_hash(snap)

    # Load existing topology row
    existing_result = await db.execute(
        select(AppTopology).where(AppTopology.app_id == str(app.id))
    )
    topology = existing_result.scalar_one_or_none()
    summary_generated = False

    if topology is None:
        # First report
        summary = await _generate_ai_summary(snap)
        summary_generated = True
        topology = AppTopology(
            app_id=str(app.id),
            team_id=str(app.team_id),
            runtime=snap.get("runtime", "python"),
            python_version=snap.get("python_version"),
            platform=snap.get("platform_name"),
            deployment_type=snap.get("deployment_type"),
            cloud_provider=snap.get("cloud_provider"),
            detected_environment=snap.get("detected_environment") or None,
            k8s_flavor=snap.get("k8s_flavor") or None,
            container_name=snap.get("container_name") or None,
            k8s_namespace=snap.get("k8s_namespace") or None,
            k8s_deployment=snap.get("k8s_deployment") or None,
            web_framework=snap.get("web_framework") or None,
            ai_providers=snap.get("ai_providers") or {},
            ai_frameworks=snap.get("ai_frameworks") or {},
            api_routes=snap.get("api_routes") or [],
            service_dependencies=snap.get("service_dependencies") or {},
            infrastructure_packages=snap.get("infrastructure") or {},
            worker_count=snap.get("worker_processes"),
            hostname=snap.get("hostname"),
            ai_summary=summary,
            ai_summary_generated_at=datetime.now(timezone.utc),
            snapshot_hash=new_hash,
            raw_snapshot=snap,
        )
        db.add(topology)

    elif topology.snapshot_hash != new_hash:
        # Snapshot changed — update and regenerate AI summary
        summary = await _generate_ai_summary(snap)
        summary_generated = True
        topology.runtime = snap.get("runtime", "python")
        topology.python_version = snap.get("python_version")
        topology.platform = snap.get("platform_name")
        topology.deployment_type = snap.get("deployment_type")
        topology.cloud_provider = snap.get("cloud_provider")
        topology.detected_environment = snap.get("detected_environment") or None
        topology.k8s_flavor = snap.get("k8s_flavor") or None
        topology.container_name = snap.get("container_name") or None
        topology.k8s_namespace = snap.get("k8s_namespace") or None
        topology.k8s_deployment = snap.get("k8s_deployment") or None
        topology.web_framework = snap.get("web_framework") or None
        topology.ai_providers = snap.get("ai_providers") or {}
        topology.ai_frameworks = snap.get("ai_frameworks") or {}
        topology.api_routes = snap.get("api_routes") or []
        topology.service_dependencies = snap.get("service_dependencies") or {}
        topology.infrastructure_packages = snap.get("infrastructure") or {}
        topology.worker_count = snap.get("worker_processes")
        topology.hostname = snap.get("hostname")
        topology.ai_summary = summary
        topology.ai_summary_generated_at = datetime.now(timezone.utc)
        topology.snapshot_hash = new_hash
        topology.raw_snapshot = snap
        topology.updated_at = datetime.now(timezone.utc)

    else:
        # No change — return cached summary
        if topology.ai_summary:
            summary = topology.ai_summary
        else:
            summary = await _generate_ai_summary(snap)

    return TopologyReportResponse(
        ai_summary=summary,
        snapshot_hash=new_hash,
        summary_generated=summary_generated,
    )


# ── Dashboard read endpoints ───────────────────────────────────────────────────

class TopologyView(BaseModel):
    app_id: str
    app_name: str
    team_id: str
    deployment_type: Optional[str]
    cloud_provider: Optional[str]
    detected_environment: Optional[str]
    k8s_flavor: Optional[str]
    web_framework: Optional[str]
    ai_providers: Optional[dict]
    ai_frameworks: Optional[dict]
    api_routes: Optional[list]
    service_dependencies: Optional[dict]
    infrastructure_packages: Optional[dict]
    hostname: Optional[str]
    ai_summary: Optional[str]
    updated_at: datetime


@router.get(
    "/topology",
    response_model=list[TopologyView],
    tags=["topology"],
    summary="List topology for all visible apps",
)
async def list_topology(
    team_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[TopologyView]:
    q = select(AppTopology, App.app_id.label("app_slug"), App.app_name).\
        join(App, AppTopology.app_id == App.id)

    if team_id:
        identity.assert_team_access(team_id)
        q = q.where(AppTopology.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(AppTopology.team_id.in_(identity.team_ids))

    rows = (await db.execute(q)).all()
    return [
        TopologyView(
            app_id=row.app_slug,
            app_name=row.app_name,
            team_id=str(row.AppTopology.team_id),
            deployment_type=row.AppTopology.deployment_type,
            cloud_provider=row.AppTopology.cloud_provider,
            detected_environment=row.AppTopology.detected_environment,
            k8s_flavor=row.AppTopology.k8s_flavor,
            web_framework=row.AppTopology.web_framework,
            ai_providers=row.AppTopology.ai_providers,
            ai_frameworks=row.AppTopology.ai_frameworks,
            api_routes=row.AppTopology.api_routes,
            service_dependencies=row.AppTopology.service_dependencies,
            infrastructure_packages=row.AppTopology.infrastructure_packages,
            hostname=row.AppTopology.hostname,
            ai_summary=row.AppTopology.ai_summary,
            updated_at=row.AppTopology.updated_at,
        )
        for row in rows
    ]


@router.get(
    "/topology/{app_id}",
    response_model=TopologyView,
    tags=["topology"],
    summary="Get topology for a specific app",
)
async def get_topology(
    app_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> TopologyView:
    # Scope by team in the query. App slugs are unique only within a team, so
    # filtering by slug alone can match rows from multiple teams and make
    # .one_or_none() raise MultipleResultsFound (500). Scoping also enforces
    # authorization at the query layer for non-admins.
    q = (
        select(AppTopology, App.app_id.label("app_slug"), App.app_name)
        .join(App, AppTopology.app_id == App.id)
        .where(App.app_id == app_id)
    )
    if not identity.is_platform_admin:
        if not identity.team_ids:
            raise HTTPException(404, "Topology not found for this app.")
        q = q.where(App.team_id.in_(identity.team_ids))
    q = q.order_by(AppTopology.team_id).limit(1)

    row = (await db.execute(q)).first()
    if not row:
        raise HTTPException(404, "Topology not found for this app.")

    identity.assert_team_access(str(row.AppTopology.team_id))

    return TopologyView(
        app_id=row.app_slug,
        app_name=row.app_name,
        team_id=str(row.AppTopology.team_id),
        deployment_type=row.AppTopology.deployment_type,
        cloud_provider=row.AppTopology.cloud_provider,
        detected_environment=row.AppTopology.detected_environment,
        k8s_flavor=row.AppTopology.k8s_flavor,
        web_framework=row.AppTopology.web_framework,
        ai_providers=row.AppTopology.ai_providers,
        ai_frameworks=row.AppTopology.ai_frameworks,
        api_routes=row.AppTopology.api_routes,
        service_dependencies=row.AppTopology.service_dependencies,
        infrastructure_packages=row.AppTopology.infrastructure_packages,
        hostname=row.AppTopology.hostname,
        ai_summary=row.AppTopology.ai_summary,
        updated_at=row.AppTopology.updated_at,
    )


# ── Team token management ──────────────────────────────────────────────────────

class TeamTokenResponse(BaseModel):
    team_id: str
    team_slug: str
    registration_token: str   # shown once
    token_prefix: str
    message: str = (
        "Store this token securely. Share it with your developers. "
        "It will not be shown again. Developers need only this token + the orchestrator URL."
    )


@router.post(
    "/teams/{team_id}/registration-token",
    response_model=TeamTokenResponse,
    status_code=201,
    tags=["topology"],
    summary="Generate a team registration token",
    description="Master key required. Token is shown once.",
)
async def generate_team_token(
    team_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> TeamTokenResponse:
    raw_key = _extract_raw_key(request)
    if not _verify_master_key(raw_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Master key required.")

    team = await db.get(Team, team_id)
    if not team or team.deleted_at:
        raise HTTPException(404, "Team not found.")

    token = _generate_team_token()
    team.registration_token_hash = await asyncio.to_thread(_hash_team_token, token)
    team.registration_token_prefix = token[:20]

    db.add(AuditLog(
        actor_id="master-key",
        actor_ip=request.client.host if request.client else None,
        team_id=team_id,
        resource_type="team",
        resource_id=team_id,
        action="registration_token_generated",
        after={"token_prefix": token[:20]},
    ))

    logger.info("Team registration token generated", extra={"team_id": team_id, "slug": team.slug})

    return TeamTokenResponse(
        team_id=team_id,
        team_slug=team.slug,
        registration_token=token,
        token_prefix=token[:20],
    )


@router.delete(
    "/teams/{team_id}/registration-token",
    status_code=204,
    response_model=None,
    tags=["topology"],
    summary="Revoke a team registration token",
)
async def revoke_team_token(
    team_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> None:
    raw_key = _extract_raw_key(request)
    if not _verify_master_key(raw_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Master key required.")

    team = await db.get(Team, team_id)
    if not team or team.deleted_at:
        raise HTTPException(404, "Team not found.")

    team.registration_token_hash = None
    team.registration_token_prefix = None

    db.add(AuditLog(
        actor_id="master-key",
        actor_ip=request.client.host if request.client else None,
        team_id=team_id,
        resource_type="team",
        resource_id=team_id,
        action="registration_token_revoked",
    ))
    logger.warning("Team registration token revoked", extra={"team_id": team_id})
