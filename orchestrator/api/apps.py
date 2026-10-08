"""
Modus — Apps API Router
================================
POST   /api/v1/apps/register          — register a new app (master key)
POST   /api/v1/apps/{id}/rotate-key   — rotate an app's API key (master key)
GET    /api/v1/apps                   — list apps (dashboard auth)
GET    /api/v1/apps/{id}              — get app detail (dashboard auth)
PATCH  /api/v1/apps/{id}             — update app metadata (dashboard auth)
DELETE /api/v1/apps/{id}             — soft-delete an app (dashboard auth)
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.config import settings
from orchestrator.db.models import App, AuditLog, Team
from orchestrator.db.session import get_session
from orchestrator.metrics.prometheus import KEY_ROTATIONS_TOTAL, REGISTRATIONS_TOTAL

logger = logging.getLogger(__name__)
router = APIRouter()

_MASTER_KEY_BCRYPT_ROUNDS = 4
# 4 rounds for master key verification — fast is fine here because master key
# requests are rare (registration only) and the key is long.
_APP_KEY_BCRYPT_ROUNDS = 12


# ── Key generation ─────────────────────────────────────────────────────────────

_ALPHABET = string.ascii_letters + string.digits

def _generate_app_key() -> str:
    """Generate a mds_ API key. URL-safe, 44 chars total."""
    token = "".join(secrets.choice(_ALPHABET) for _ in range(32))
    return f"{settings.api_key_prefix}{token}"


def _hash_key(key: str, rounds: int = _APP_KEY_BCRYPT_ROUNDS) -> str:
    """Bcrypt hash a key for storage. Returns the hash string."""
    return bcrypt.hashpw(key.encode(), bcrypt.gensalt(rounds=rounds)).decode()


def _verify_master_key(provided: str) -> bool:
    """Constant-time comparison for master key check."""
    from orchestrator.core.auth import verify_master_key
    return verify_master_key(provided)


def _extract_raw_key(request: Request) -> str:
    key = (
        request.headers.get("X-Modus-APIKey", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    return key


# ── Schemas ────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    app_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    app_name: str = Field(..., min_length=1, max_length=256)
    team_slug: str = Field(..., min_length=1, max_length=64)
    environment: str = Field("production", pattern=r"^(production|staging|dev)$")


class RegisterResponse(BaseModel):
    app_id: str
    app_name: str
    team_slug: str
    environment: str
    api_key: str  # shown once — never stored in plaintext
    api_key_prefix: str
    message: str = "Registration successful. Store the api_key — it will not be shown again."


class RotateKeyResponse(BaseModel):
    app_id: str
    api_key: str
    api_key_prefix: str
    message: str = "Key rotated. Store the new api_key — it will not be shown again."


class AppResponse(BaseModel):
    id: str
    app_id: str
    app_name: str
    team_id: str
    team_slug: Optional[str] = None
    environment: str
    api_key_prefix: str
    agent_version: Optional[str]
    sdk_versions: Optional[dict]
    last_seen_at: Optional[datetime]
    first_seen_at: Optional[datetime]
    is_active: bool
    created_at: datetime


class AppUpdateRequest(BaseModel):
    app_name: Optional[str] = Field(None, max_length=256)
    environment: Optional[str] = Field(None, pattern=r"^(production|staging|dev)$")
    is_active: Optional[bool] = None


# ── Registration ───────────────────────────────────────────────────────────────

@router.post(
    "/apps/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new application",
)
async def register_app(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> RegisterResponse:
    """
    Register a new application and issue an API key.

    Requires the master key in X-Modus-APIKey header.
    The returned api_key is shown once and not stored — save it immediately.

    The app row (and an auto-created team) is committed before the response
    is sent, so the app is listed and its key works on the very next request.
    """
    raw_key = _extract_raw_key(request)
    if not _verify_master_key(raw_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master key required for app registration.",
        )

    # Resolve team — auto-create if it doesn't exist (this is a read + rare write)
    team_result = await db.execute(
        select(Team).where(Team.slug == body.team_slug, Team.deleted_at.is_(None))
    )
    team = team_result.scalar_one_or_none()

    if team is None:
        logger.info(
            "Auto-creating team on first registration",
            extra={"team_slug": body.team_slug},
        )
        team = Team(
            slug=body.team_slug,
            name=body.team_slug.replace("-", " ").replace("_", " ").title(),
        )
        db.add(team)
        await db.flush()  # Get team.id before using it

    team_id = str(team.id)

    # Check for existing app in DB
    existing_result = await db.execute(
        select(App).where(
            App.team_id == team_id,
            App.app_id == body.app_id,
            App.deleted_at.is_(None),
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"App '{body.app_id}' is already registered in team '{body.team_slug}'. "
                f"Use POST /api/v1/apps/{{id}}/rotate-key to issue a new key."
            ),
        )

    # Generate key upfront — returned to caller once the row is committed
    import uuid as _uuid
    api_key = _generate_app_key()
    api_key_hash = _hash_key(api_key)
    api_key_prefix = api_key[:16]
    app_uuid = str(_uuid.uuid4())

    # Write the app and its audit entry in this request's transaction and
    # commit before responding. (Registration used to be queued for the
    # background writer: the 201 arrived before the row existed, so the app
    # was missing from GET /apps, and a write failure after the response,
    # e.g. an app_id reused after a soft delete, silently voided the key.)
    db.add(App(
        id=app_uuid,
        team_id=team_id,
        app_id=body.app_id,
        app_name=body.app_name,
        environment=body.environment,
        api_key_hash=api_key_hash,
        api_key_prefix=api_key_prefix,
    ))
    db.add(AuditLog(
        actor_id="master-key",
        actor_ip=request.client.host if request.client else None,
        team_id=team_id,
        resource_type="app",
        resource_id=app_uuid,
        action="registered",
        after={"app_id": body.app_id, "team": body.team_slug, "environment": body.environment},
    ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"App '{body.app_id}' is already registered in team '{body.team_slug}' "
                f"(or was deleted and its id is still reserved). "
                f"Use POST /api/v1/apps/{{id}}/rotate-key to issue a new key."
            ),
        )

    # Warm the ingest key cache so the first SDK call skips the bcrypt check
    # (the row itself is already committed).
    from orchestrator.api.ingest import pre_cache_registration
    pre_cache_registration(
        app_uuid=app_uuid,
        app_id=body.app_id,
        team_id=team_id,
        environment=body.environment,
        api_key=api_key,
        api_key_prefix=api_key_prefix,
        api_key_hash=api_key_hash,
    )

    REGISTRATIONS_TOTAL.labels(environment=body.environment).inc()

    logger.info(
        "App registered",
        extra={
            "app_id": body.app_id,
            "team": body.team_slug,
            "environment": body.environment,
        },
    )

    return RegisterResponse(
        app_id=body.app_id,
        app_name=body.app_name,
        team_slug=body.team_slug,
        environment=body.environment,
        api_key=api_key,
        api_key_prefix=api_key_prefix,
    )


# ── Key rotation ───────────────────────────────────────────────────────────────

@router.post(
    "/apps/{app_uuid}/rotate-key",
    response_model=RotateKeyResponse,
    summary="Rotate an app's API key",
)
async def rotate_key(
    app_uuid: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> RotateKeyResponse:
    """Rotate the API key for an app. Requires master key. Old key immediately invalid."""
    raw_key = _extract_raw_key(request)
    if not _verify_master_key(raw_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master key required for key rotation.",
        )

    app = await db.get(App, app_uuid)
    if app is None or app.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found.")

    old_prefix = app.api_key_prefix
    new_key = _generate_app_key()
    app.api_key_hash = _hash_key(new_key)
    app.api_key_prefix = new_key[:16]

    db.add(AuditLog(
        actor_id="master-key",
        actor_ip=request.client.host if request.client else None,
        team_id=str(app.team_id),
        resource_type="api_key",
        resource_id=str(app.id),
        action="key_rotated",
        before={"api_key_prefix": old_prefix},
        after={"api_key_prefix": app.api_key_prefix},
    ))

    KEY_ROTATIONS_TOTAL.inc()

    logger.info("API key rotated", extra={"app_id": app.app_id})

    return RotateKeyResponse(
        app_id=app.app_id,
        api_key=new_key,
        api_key_prefix=app.api_key_prefix,
    )


# ── List / Get / Update / Delete ───────────────────────────────────────────────

@router.get("/apps", response_model=list[AppResponse], summary="List apps")
async def list_apps(
    team_id: Optional[str] = None,
    environment: Optional[str] = None,
    active_only: bool = True,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[AppResponse]:
    identity.assert_permission("apps:read")
    query = select(App, Team.slug).join(Team, App.team_id == Team.id).where(
        App.deleted_at.is_(None)
    )

    if active_only:
        query = query.where(App.is_active == True)

    # Team scoping — enforced via identity
    if not identity.is_platform_admin and identity.team_ids:
        query = query.where(App.team_id.in_(identity.team_ids))
    elif team_id:
        identity.assert_team_access(team_id)
        query = query.where(App.team_id == team_id)

    if environment:
        query = query.where(App.environment == environment)

    result = await db.execute(
        query.order_by(App.created_at.desc()).limit(limit).offset(offset)
    )
    rows = result.all()

    return [
        AppResponse(
            id=str(app.id),
            app_id=app.app_id,
            app_name=app.app_name,
            team_id=str(app.team_id),
            team_slug=team_slug,
            environment=app.environment,
            api_key_prefix=app.api_key_prefix,
            agent_version=app.agent_version,
            sdk_versions=app.sdk_versions,
            last_seen_at=app.last_seen_at,
            first_seen_at=app.first_seen_at,
            is_active=app.is_active,
            created_at=app.created_at,
        )
        for app, team_slug in rows
    ]


@router.get("/apps/{app_uuid}", response_model=AppResponse, summary="Get app detail")
async def get_app(
    app_uuid: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> AppResponse:
    result = await db.execute(
        select(App, Team.slug).join(Team).where(
            App.id == app_uuid,
            App.deleted_at.is_(None),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="App not found.")

    app, team_slug = row
    identity.assert_team_access(str(app.team_id))

    return AppResponse(
        id=str(app.id),
        app_id=app.app_id,
        app_name=app.app_name,
        team_id=str(app.team_id),
        team_slug=team_slug,
        environment=app.environment,
        api_key_prefix=app.api_key_prefix,
        agent_version=app.agent_version,
        sdk_versions=app.sdk_versions,
        last_seen_at=app.last_seen_at,
        first_seen_at=app.first_seen_at,
        is_active=app.is_active,
        created_at=app.created_at,
    )


@router.patch("/apps/{app_uuid}", response_model=AppResponse, summary="Update app")
async def update_app(
    app_uuid: str,
    body: AppUpdateRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> AppResponse:
    identity.assert_permission("apps:write")

    result = await db.execute(
        select(App, Team.slug).join(Team).where(
            App.id == app_uuid, App.deleted_at.is_(None)
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="App not found.")

    app, team_slug = row
    identity.assert_team_access(str(app.team_id))

    before = {"app_name": app.app_name, "environment": app.environment, "is_active": app.is_active}

    if body.app_name is not None:
        app.app_name = body.app_name
    if body.environment is not None:
        app.environment = body.environment
    if body.is_active is not None:
        app.is_active = body.is_active

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=str(app.team_id),
        resource_type="app",
        resource_id=str(app.id),
        action="updated",
        before=before,
        after=body.model_dump(exclude_none=True),
    ))

    return AppResponse(
        id=str(app.id), app_id=app.app_id, app_name=app.app_name,
        team_id=str(app.team_id), team_slug=team_slug, environment=app.environment,
        api_key_prefix=app.api_key_prefix, agent_version=app.agent_version,
        sdk_versions=app.sdk_versions, last_seen_at=app.last_seen_at,
        first_seen_at=app.first_seen_at, is_active=app.is_active, created_at=app.created_at,
    )


@router.delete(
    "/apps/{app_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Deactivate app (soft delete)",
)
async def delete_app(
    app_uuid: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> None:
    identity.assert_permission("apps:delete")

    app = await db.get(App, app_uuid)
    if app is None or app.deleted_at is not None:
        raise HTTPException(status_code=404, detail="App not found.")

    identity.assert_team_access(str(app.team_id))

    app.deleted_at = datetime.now(timezone.utc)
    app.is_active = False

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=str(app.team_id),
        resource_type="app",
        resource_id=str(app.id),
        action="deleted",
        before={"app_id": app.app_id},
    ))

    logger.info("App deactivated", extra={"app_id": app.app_id, "actor": identity.actor_id})
