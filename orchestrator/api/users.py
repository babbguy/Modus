"""
Modus — Users Router
===========================
User management, invitations, and preferences.

Endpoints:
    GET    /api/v1/users                    — List users
    GET    /api/v1/users/{user_id}          — Get user detail
    PATCH  /api/v1/users/{user_id}          — Update user (activate/deactivate)
    DELETE /api/v1/users/{user_id}          — Delete user

    POST   /api/v1/users/invite             — Send invitation
    GET    /api/v1/users/invitations        — List pending invitations
    POST   /api/v1/users/invitations/{id}/revoke — Revoke invitation
    POST   /api/v1/users/invitations/accept — Accept invitation (public)

    GET    /api/v1/users/me/preferences     — Get current user preferences
    PUT    /api/v1/users/me/preferences     — Update current user preferences

    GET    /api/v1/users/invite/channels    — List configured channels
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.invitation_service import (
    InvitationMessage,
    get_configured_channels,
    get_provider,
)
from orchestrator.core.permissions import clear_permissions_cache
from orchestrator.db.models import (
    AuditLog,
    Invitation,
    RbacAssignment,
    RbacRole,
    Team,
    TeamMembership,
    User,
    UserPreference,
)
from orchestrator.db.session import get_session

router = APIRouter()


# ── Response / Request models ─────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    avatar_url: Optional[str] = None
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=256)
    is_active: Optional[bool] = None
    avatar_url: Optional[str] = None


class InviteRequest(BaseModel):
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    display_name: str = Field(..., min_length=1, max_length=256)
    role_id: str
    team_id: Optional[str] = None
    channel: str = Field("email", pattern=r"^(email|slack|teams)$")


class InvitationResponse(BaseModel):
    id: str
    email: str
    role_name: str
    team_name: Optional[str] = None
    channel: str
    status: str
    invited_by_name: str
    expires_at: datetime
    created_at: datetime


class AcceptInvitationRequest(BaseModel):
    token: str
    display_name: Optional[str] = None


class PreferencesResponse(BaseModel):
    default_team_id: Optional[str] = None
    default_view: Optional[str] = None
    pinned_app_ids: list[str] = []
    dashboard_layout: Optional[dict] = None
    notification_prefs: dict = {}
    timezone: Optional[str] = "UTC"
    theme: Optional[str] = "system"


class PreferencesUpdateRequest(BaseModel):
    default_team_id: Optional[str] = None
    default_view: Optional[str] = Field(None, pattern=r"^(overview|devops|executive|billing)$")
    pinned_app_ids: Optional[list[str]] = None
    dashboard_layout: Optional[dict] = None
    notification_prefs: Optional[dict] = None
    timezone: Optional[str] = None
    theme: Optional[str] = Field(None, pattern=r"^(light|dark|system)$")


class ChannelsResponse(BaseModel):
    configured: list[str]
    all: list[str]


# ── User CRUD ─────────────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserResponse])
async def list_users(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    is_active: Optional[bool] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[UserResponse]:
    identity.assert_permission("users:read")
    q = select(User)
    if is_active is not None:
        q = q.where(User.is_active == is_active)
    rows = (await db.execute(
        q.order_by(User.display_name).limit(limit).offset(offset)
    )).scalars().all()
    return [UserResponse(
        id=str(u.id), email=u.email, display_name=u.display_name,
        avatar_url=u.avatar_url, is_active=u.is_active,
        last_login_at=u.last_login_at, created_at=u.created_at,
    ) for u in rows]


# ── Current User (self) ──────────────────────────────────────────────────────
#
# These endpoints back the dashboard Profile view (dashboard/js/views/profile.js).
# They MUST be declared before the /users/{user_id} parameterised routes,
# otherwise FastAPI matches "me" as a {user_id} value and returns 404.
# Added 2026-04-08 ahead of demo walkthrough.

class MeResponse(BaseModel):
    id: Optional[str] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    role: Optional[str] = None
    avatar_url: Optional[str] = None
    team_ids: list[str] = []


class MeUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


@router.get("/users/me", response_model=MeResponse)
async def get_me(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> MeResponse:
    """Return the current authenticated user's profile.

    Falls back to identity-only fields when the deployment is in stub
    auth mode and there is no User row.
    """
    if not identity.user_id:
        return MeResponse(
            email=identity.email,
            display_name=identity.email or identity.actor_id,
            role=identity.role,
            team_ids=list(identity.team_ids),
        )

    user = (await db.execute(
        select(User).where(User.id == identity.user_id)
    )).scalar_one_or_none()

    if not user:
        return MeResponse(
            email=identity.email,
            display_name=identity.email or identity.actor_id,
            role=identity.role,
            team_ids=list(identity.team_ids),
        )

    return MeResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=identity.role,
        avatar_url=user.avatar_url,
        team_ids=list(identity.team_ids),
    )


@router.put("/users/me", response_model=MeResponse)
async def update_me(
    body: MeUpdateRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> MeResponse:
    """Update the current user's display name and/or email."""
    if not identity.user_id:
        return MeResponse(
            email=body.email or identity.email,
            display_name=body.display_name or identity.email or identity.actor_id,
            role=identity.role,
            team_ids=list(identity.team_ids),
        )

    user = (await db.execute(
        select(User).where(User.id == identity.user_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found.")

    if body.display_name is not None:
        user.display_name = body.display_name
    if body.email is not None:
        user.email = body.email
    await db.flush()

    return MeResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=identity.role,
        avatar_url=user.avatar_url,
        team_ids=list(identity.team_ids),
    )


@router.put("/users/me/password", status_code=501, response_model=None)
async def change_my_password(
    body: PasswordChangeRequest,
    identity: Identity = Depends(get_identity),
):
    """Change the current user's password. In stub mode and SSO mode this
    is delegated to the upstream identity provider — returns 501 so the
    dashboard can show 'not available in this deployment'."""
    raise HTTPException(
        status_code=501,
        detail="Password changes are managed by your identity provider.",
    )


@router.post("/users/me/api-key", status_code=501, response_model=None)
async def rotate_my_api_key(
    identity: Identity = Depends(get_identity),
):
    """Rotate the current user's personal API key. Personal keys are not
    yet implemented; app-level keys are rotated via
    /api/v1/apps/{id}/rotate-key."""
    raise HTTPException(
        status_code=501,
        detail="Personal API keys not yet supported. Use app-level keys.",
    )


# NOTE: declared before /users/{user_id} so "invitations" is not captured as a user id.
@router.get("/users/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    status_filter: Optional[str] = Query(None, alias="status",
                                          pattern=r"^(pending|accepted|expired|revoked)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[InvitationResponse]:
    identity.assert_permission("users:invite")

    q = select(Invitation)
    if status_filter:
        q = q.where(Invitation.status == status_filter)
    q = q.order_by(Invitation.created_at.desc()).limit(limit).offset(offset)

    rows = (await db.execute(q)).scalars().all()
    results = []
    for inv in rows:
        role = (await db.execute(
            select(RbacRole).where(RbacRole.id == inv.role_id)
        )).scalar_one_or_none()
        team_name = None
        if inv.team_id:
            team = (await db.execute(
                select(Team).where(Team.id == inv.team_id)
            )).scalar_one_or_none()
            if team:
                team_name = team.name

        inviter_name = str(inv.invited_by)
        inviter = (await db.execute(
            select(User).where(User.id == str(inv.invited_by))
        )).scalar_one_or_none()
        if inviter:
            inviter_name = inviter.display_name

        results.append(InvitationResponse(
            id=str(inv.id), email=inv.email,
            role_name=role.name if role else "unknown",
            team_name=team_name, channel=inv.channel,
            status=inv.status, invited_by_name=inviter_name,
            expires_at=inv.expires_at, created_at=inv.created_at,
        ))
    return results


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> UserResponse:
    identity.assert_permission("users:read")
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found.")
    return UserResponse(
        id=str(user.id), email=user.email, display_name=user.display_name,
        avatar_url=user.avatar_url, is_active=user.is_active,
        last_login_at=user.last_login_at, created_at=user.created_at,
    )


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> UserResponse:
    identity.assert_permission("users:write")
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found.")

    before = {"display_name": user.display_name, "is_active": user.is_active}
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.avatar_url is not None:
        user.avatar_url = body.avatar_url

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="user", resource_id=user_id,
        action="updated", before=before,
        after=body.model_dump(exclude_none=True),
    ))

    # Clear permission cache if activation status changed
    if body.is_active is not None:
        clear_permissions_cache(user_id)

    return UserResponse(
        id=str(user.id), email=user.email, display_name=user.display_name,
        avatar_url=user.avatar_url, is_active=user.is_active,
        last_login_at=user.last_login_at, created_at=user.created_at,
    )


@router.delete("/users/{user_id}", status_code=204, response_model=None)
async def delete_user(
    user_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    identity.assert_permission("users:delete")
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found.")

    # Prevent self-deletion
    if identity.user_id and identity.user_id == user_id:
        raise HTTPException(400, "Cannot delete your own account.")

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="user", resource_id=user_id,
        action="deleted", before={"email": user.email},
    ))

    await db.delete(user)
    clear_permissions_cache(user_id)


# ── Invitations ───────────────────────────────────────────────────────────────

@router.get("/users/invite/channels", response_model=ChannelsResponse)
async def list_invite_channels(
    identity: Identity = Depends(get_identity),
) -> ChannelsResponse:
    identity.assert_permission("users:invite")
    return ChannelsResponse(
        configured=get_configured_channels(),
        all=["email", "slack", "teams"],
    )


@router.post("/users/invite", response_model=InvitationResponse, status_code=201)
async def invite_user(
    body: InviteRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> InvitationResponse:
    identity.assert_permission("users:invite")

    # Check if user already exists
    existing = (await db.execute(
        select(User).where(User.email == body.email)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"User with email '{body.email}' already exists.")

    # Check for existing pending invitation
    pending = (await db.execute(
        select(Invitation).where(
            Invitation.email == body.email,
            Invitation.status == "pending",
        )
    )).scalar_one_or_none()
    if pending:
        raise HTTPException(409, "A pending invitation already exists for this email.")

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

    # Get inviter info
    inviter_name = identity.email or identity.actor_id
    if identity.user_id:
        inviter = (await db.execute(
            select(User).where(User.id == identity.user_id)
        )).scalar_one_or_none()
        if inviter:
            inviter_name = inviter.display_name

    # Generate invitation token
    token = f"mds_invite_{secrets.token_urlsafe(32)}"
    token_hash = (await asyncio.to_thread(bcrypt.hashpw, token.encode(), bcrypt.gensalt(12))).decode()
    token_prefix = token[:20]

    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    invitation = Invitation(
        email=body.email,
        role_id=body.role_id,
        team_id=body.team_id,
        invited_by=identity.user_id or identity.actor_id,
        token_hash=token_hash,
        token_prefix=token_prefix,
        channel=body.channel,
        status="pending",
        expires_at=expires_at,
    )
    db.add(invitation)
    await db.flush()

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="invitation", resource_id=str(invitation.id),
        action="created",
        after={"email": body.email, "role": role.name,
               "team_id": body.team_id, "channel": body.channel},
    ))

    # Send invitation via selected channel
    from orchestrator.core.config import settings
    base_url = f"http{'s' if settings.environment == 'production' else ''}://{settings.host}:{settings.port}"
    invite_url = f"{base_url}/invite/accept?token={token}"

    provider = get_provider(body.channel)
    msg = InvitationMessage(
        recipient_email=body.email,
        recipient_name=body.display_name,
        inviter_name=inviter_name,
        role_name=role.name,
        team_name=team_name,
        invite_url=invite_url,
    )

    if provider.is_configured():
        sent = await provider.send(msg)
        if not sent:
            logger.warning("Invitation created but delivery failed for %s via %s",
                           body.email, body.channel)
    else:
        logger.warning("Channel '%s' is not configured. Invitation created but not sent.",
                       body.channel)

    return InvitationResponse(
        id=str(invitation.id), email=body.email,
        role_name=role.name, team_name=team_name,
        channel=body.channel, status="pending",
        invited_by_name=inviter_name,
        expires_at=expires_at, created_at=invitation.created_at,
    )


@router.post("/users/invitations/{invitation_id}/revoke", status_code=204, response_model=None)
async def revoke_invitation(
    invitation_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    identity.assert_permission("users:invite")
    inv = (await db.execute(
        select(Invitation).where(Invitation.id == invitation_id)
    )).scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Invitation not found.")
    if inv.status != "pending":
        raise HTTPException(400, f"Cannot revoke invitation with status '{inv.status}'.")

    inv.status = "revoked"
    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="invitation", resource_id=invitation_id,
        action="revoked", before={"status": "pending"},
        after={"status": "revoked"},
    ))


@router.post("/users/invitations/accept", response_model=UserResponse, status_code=201)
async def accept_invitation(
    body: AcceptInvitationRequest,
    request: Request,
    db: AsyncSession = Depends(get_session, scope="function"),
) -> UserResponse:
    """
    Public endpoint — no auth required. Token serves as authentication.
    Creates the user account and assigns the role from the invitation.
    """
    if not body.token.startswith("mds_invite_"):
        raise HTTPException(400, "Invalid invitation token format.")

    # Filter by token prefix to avoid brute-force bcrypt over all invitations
    token_prefix = body.token[:20]
    pending = (await db.execute(
        select(Invitation).where(
            Invitation.status == "pending",
            Invitation.token_prefix == token_prefix,
        )
    )).scalars().all()

    matched_inv = None
    for inv in pending:
        if await asyncio.to_thread(bcrypt.checkpw, body.token.encode(), inv.token_hash.encode()):
            matched_inv = inv
            break

    if not matched_inv:
        raise HTTPException(404, "Invalid or expired invitation token.")

    if matched_inv.expires_at < datetime.now(timezone.utc):
        matched_inv.status = "expired"
        raise HTTPException(410, "This invitation has expired.")

    # Check if user already exists (e.g. accepted via different invitation)
    existing = (await db.execute(
        select(User).where(User.email == matched_inv.email)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "An account with this email already exists.")

    # Create user
    display_name = body.display_name or matched_inv.email.split("@")[0]
    user = User(
        email=matched_inv.email,
        display_name=display_name,
    )
    db.add(user)
    await db.flush()

    # Create RBAC assignment
    assignment = RbacAssignment(
        user_id=str(user.id),
        role_id=matched_inv.role_id,
        team_id=matched_inv.team_id,
        assigned_by=str(matched_inv.invited_by),
    )
    db.add(assignment)

    # Add team membership if team-scoped
    if matched_inv.team_id:
        membership = TeamMembership(
            user_id=str(user.id),
            team_id=matched_inv.team_id,
            added_by=str(matched_inv.invited_by),
        )
        db.add(membership)

    # Create default preferences
    prefs = UserPreference(
        user_id=str(user.id),
        default_team_id=matched_inv.team_id,
    )
    db.add(prefs)

    # Mark invitation as accepted
    matched_inv.status = "accepted"
    matched_inv.accepted_at = datetime.now(timezone.utc)

    db.add(AuditLog(
        actor_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        resource_type="invitation", resource_id=str(matched_inv.id),
        action="accepted",
        after={"user_id": str(user.id), "email": user.email},
    ))

    return UserResponse(
        id=str(user.id), email=user.email, display_name=user.display_name,
        avatar_url=user.avatar_url, is_active=user.is_active,
        last_login_at=user.last_login_at, created_at=user.created_at,
    )


# ── User Preferences ─────────────────────────────────────────────────────────

@router.get("/users/me/preferences", response_model=PreferencesResponse)
async def get_my_preferences(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> PreferencesResponse:
    if not identity.user_id:
        raise HTTPException(400, "Preferences require an authenticated user account.")

    prefs = (await db.execute(
        select(UserPreference).where(UserPreference.user_id == identity.user_id)
    )).scalar_one_or_none()

    if not prefs:
        return PreferencesResponse()

    return PreferencesResponse(
        default_team_id=str(prefs.default_team_id) if prefs.default_team_id else None,
        default_view=prefs.default_view,
        pinned_app_ids=prefs.pinned_app_ids or [],
        dashboard_layout=prefs.dashboard_layout,
        notification_prefs=prefs.notification_prefs or {},
        timezone=prefs.timezone,
        theme=prefs.theme,
    )


@router.put("/users/me/preferences", response_model=PreferencesResponse)
async def update_my_preferences(
    body: PreferencesUpdateRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> PreferencesResponse:
    if not identity.user_id:
        raise HTTPException(400, "Preferences require an authenticated user account.")

    prefs = (await db.execute(
        select(UserPreference).where(UserPreference.user_id == identity.user_id)
    )).scalar_one_or_none()

    if not prefs:
        prefs = UserPreference(user_id=identity.user_id)
        db.add(prefs)

    if body.default_team_id is not None:
        # Validate user has access to this team
        identity.assert_team_access(body.default_team_id)
        prefs.default_team_id = body.default_team_id
    if body.default_view is not None:
        # Validate user has permission for this view
        view_perm = f"view:{body.default_view}"
        if not identity.has_permission(view_perm):
            raise HTTPException(403, f"You don't have access to the {body.default_view} view.")
        prefs.default_view = body.default_view
    if body.pinned_app_ids is not None:
        prefs.pinned_app_ids = body.pinned_app_ids
    if body.dashboard_layout is not None:
        prefs.dashboard_layout = body.dashboard_layout
    if body.notification_prefs is not None:
        prefs.notification_prefs = body.notification_prefs
    if body.timezone is not None:
        prefs.timezone = body.timezone
    if body.theme is not None:
        prefs.theme = body.theme

    await db.flush()

    return PreferencesResponse(
        default_team_id=str(prefs.default_team_id) if prefs.default_team_id else None,
        default_view=prefs.default_view,
        pinned_app_ids=prefs.pinned_app_ids or [],
        dashboard_layout=prefs.dashboard_layout,
        notification_prefs=prefs.notification_prefs or {},
        timezone=prefs.timezone,
        theme=prefs.theme,
    )


