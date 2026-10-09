"""
Modus — SCIM 2.0 Provisioning API
=========================================
RFC 7644 compliant endpoints for enterprise identity provider integration.

Supports user provisioning and deprovisioning from Okta, Azure AD,
OneLogin, JumpCloud, and any SCIM 2.0-compatible IdP.

Endpoints:
    GET    /api/v1/scim/v2/ServiceProviderConfig   — SCIM capabilities
    GET    /api/v1/scim/v2/Schemas                 — Supported schemas
    GET    /api/v1/scim/v2/ResourceTypes           — Supported resource types

    GET    /api/v1/scim/v2/Users                   — List/filter users
    POST   /api/v1/scim/v2/Users                   — Create user
    GET    /api/v1/scim/v2/Users/{id}              — Get user
    PUT    /api/v1/scim/v2/Users/{id}              — Replace user
    PATCH  /api/v1/scim/v2/Users/{id}              — Partial update user
    DELETE /api/v1/scim/v2/Users/{id}              — Deactivate user

    GET    /api/v1/scim/v2/Groups                  — List/filter groups (roles)
    POST   /api/v1/scim/v2/Groups                  — Create group (custom role)
    GET    /api/v1/scim/v2/Groups/{id}             — Get group
    PUT    /api/v1/scim/v2/Groups/{id}             — Replace group
    PATCH  /api/v1/scim/v2/Groups/{id}             — Partial update group
    DELETE /api/v1/scim/v2/Groups/{id}             — Delete group
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.permissions import clear_permissions_cache
from orchestrator.db.models import (
    AuditLog,
    RbacAssignment,
    RbacRole,
    User,
)
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)

scim_router = APIRouter(prefix="/scim/v2", tags=["scim"])

SCIM_CONTENT_TYPE = "application/scim+json"

# Core + extension schema URNs (case-sensitive per RFC 7643 §10).
CORE_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
CORE_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE_USER_SCHEMA = (
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
)

# ── Enterprise User extension (durable) ──────────────────────────────────────
#
# Microsoft Entra (and Okta, when the enterprise extension is enabled) send and
# expect the enterprise-user extension:
#   urn:ietf:params:scim:schemas:extension:enterprise:2.0:User
# with sub-attributes: employeeNumber, costCenter, organization, division,
# department, manager{value,$ref,displayName}.
#
# None of these map to a first-class User column, so the whole extension is
# persisted in the User.scim_enterprise_ext JSON column. It durably round-trips across restarts and workers — an IdP's
# create/PUT/PATCH is reflected back on every subsequent GET. Telemetry/metadata
# only — never PII.
#
# The helpers below operate on the in-session User ORM object and always REASSIGN
# user.scim_enterprise_ext (rather than mutating the dict in place) so SQLAlchemy
# reliably marks the column dirty and the endpoint's commit persists it.


def _set_enterprise_ext(user, ext: Optional[dict]) -> None:
    """Replace the user's enterprise extension (falsy/None clears it)."""
    user.scim_enterprise_ext = dict(ext) if ext else None


def _merge_enterprise_ext(user, partial: Optional[dict]) -> None:
    """Shallow-merge partial enterprise sub-attributes into the stored ext."""
    if not partial:
        return
    cur = dict(user.scim_enterprise_ext or {})
    cur.update(partial)
    user.scim_enterprise_ext = cur


def _merge_enterprise_ext_path(user, subpath: str, value) -> None:
    """Set a (possibly dotted, e.g. 'manager.value') enterprise sub-attribute."""
    if not subpath:
        if isinstance(value, dict):
            _merge_enterprise_ext(user, value)
        return
    cur = dict(user.scim_enterprise_ext or {})
    parts = subpath.split(".")
    node = cur
    for p in parts[:-1]:
        child = node.get(p)
        if not isinstance(child, dict):
            child = {}
        node[p] = child
        node = child
    node[parts[-1]] = value
    user.scim_enterprise_ext = cur


def _remove_enterprise_ext_path(user, subpath: str) -> None:
    """Remove an enterprise sub-attribute (or the whole extension if empty)."""
    if not subpath:
        user.scim_enterprise_ext = None
        return
    cur = dict(user.scim_enterprise_ext or {})
    parts = subpath.split(".")
    node = cur
    for p in parts[:-1]:
        child = node.get(p)
        if not isinstance(child, dict):
            return
        node = child
    node.pop(parts[-1], None)
    user.scim_enterprise_ext = cur or None


def _get_enterprise_ext(user) -> Optional[dict]:
    """Return the user's stored enterprise extension, if any."""
    return user.scim_enterprise_ext


# ── SCIM response helpers ────────────────────────────────────────────────────


def _scim_response(data: dict, status_code: int = 200) -> JSONResponse:
    """Return a JSONResponse with SCIM content type."""
    return JSONResponse(content=data, status_code=status_code,
                        media_type=SCIM_CONTENT_TYPE)


def _scim_error(status: int, detail: str, scim_type: str = "invalidValue") -> JSONResponse:
    """Return a SCIM-formatted error response."""
    return JSONResponse(
        status_code=status,
        media_type=SCIM_CONTENT_TYPE,
        content={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "detail": detail,
            "scimType": scim_type,
            "status": str(status),
        },
    )


def _user_to_scim(user: User, base_url: str = "") -> dict:
    """Convert a User model to SCIM User resource representation."""
    schemas = [CORE_USER_SCHEMA]
    resource = {
        "schemas": schemas,
        "id": str(user.id),
        "externalId": user.external_id or "",
        "userName": user.email,
        "name": {
            "formatted": user.display_name,
        },
        "displayName": user.display_name,
        "emails": [
            {
                "value": user.email,
                "type": "work",
                "primary": True,
            }
        ],
        "active": user.is_active,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else None,
            "lastModified": user.updated_at.isoformat() if user.updated_at else None,
            "location": f"{base_url}/scim/v2/Users/{user.id}",
        },
    }

    # Echo the enterprise extension back when the IdP supplied one. The URN is
    # added to `schemas` only when the extension is actually populated (RFC 7643
    # §3.3 — declared schemas must reflect present attributes).
    ext = _get_enterprise_ext(user)
    if ext:
        resource[ENTERPRISE_USER_SCHEMA] = ext
        schemas.append(ENTERPRISE_USER_SCHEMA)

    return resource


def _role_to_scim_group(role: RbacRole, members: list[dict] = None,
                         base_url: str = "") -> dict:
    """Convert an RbacRole model to SCIM Group resource representation."""
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
        "id": str(role.id),
        "displayName": role.name,
        "members": members or [],
        "meta": {
            "resourceType": "Group",
            "created": role.created_at.isoformat() if role.created_at else None,
            "lastModified": role.updated_at.isoformat() if role.updated_at else None,
            "location": f"{base_url}/scim/v2/Groups/{role.id}",
        },
    }


# ── Discovery endpoints ─────────────────────────────────────────────────────


@scim_router.get("/ServiceProviderConfig")
async def service_provider_config():
    """SCIM ServiceProviderConfig — advertises supported capabilities."""
    return _scim_response({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "documentationUri": "https://github.com/babbguy/Modus/tree/main/docs",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": "Authentication via OAuth 2.0 Bearer Token (JWT)",
            }
        ],
    })


@scim_router.get("/Schemas")
async def list_schemas():
    """SCIM Schema discovery."""
    return _scim_response({
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 2,
        "Resources": [
            {
                "id": "urn:ietf:params:scim:schemas:core:2.0:User",
                "name": "User",
                "description": "User Account",
            },
            {
                "id": "urn:ietf:params:scim:schemas:core:2.0:Group",
                "name": "Group",
                "description": "Group (mapped to RBAC roles)",
            },
        ],
    })


@scim_router.get("/ResourceTypes")
async def resource_types():
    """SCIM ResourceType discovery."""
    return _scim_response({
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 2,
        "Resources": [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "User",
                "name": "User",
                "endpoint": "/scim/v2/Users",
                "schema": "urn:ietf:params:scim:schemas:core:2.0:User",
            },
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "Group",
                "name": "Group",
                "endpoint": "/scim/v2/Groups",
                "schema": "urn:ietf:params:scim:schemas:core:2.0:Group",
            },
        ],
    })


# ── User endpoints ───────────────────────────────────────────────────────────


@scim_router.get("/Users")
async def list_users(
    request: Request,
    filter: Optional[str] = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=1, le=200),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """List or filter users (SCIM ListResponse)."""
    identity.assert_permission("users:read")

    # Parse the SCIM filter once (raising a proper 400 on anything unsupported),
    # then apply the same expression to both the page query and the count query.
    filter_expr = None
    if filter:
        try:
            filter_expr = _build_user_filter(filter)
        except ScimFilterError as exc:
            return _scim_error(400, str(exc), "invalidFilter")

    q = select(User).order_by(User.created_at)
    if filter_expr is not None:
        q = q.where(filter_expr)

    # totalResults must reflect the FILTERED set, not the whole table — an IdP
    # paging a filtered result otherwise gets an inconsistent total.
    count_q = select(func.count()).select_from(User)
    if filter_expr is not None:
        count_q = count_q.where(filter_expr)
    total = await db.scalar(count_q) or 0

    # SCIM pagination is 1-indexed
    offset = max(0, startIndex - 1)
    q = q.offset(offset).limit(count)

    result = await db.execute(q)
    users = result.scalars().all()

    base_url = str(request.base_url).rstrip("/")
    return _scim_response({
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": total,
        "startIndex": startIndex,
        "itemsPerPage": len(users),
        "Resources": [_user_to_scim(u, base_url) for u in users],
    })


@scim_router.post("/Users")
async def create_user(
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Create a user via SCIM provisioning."""
    identity.assert_permission("users:write")
    body = await request.json()

    email = _extract_email(body)
    if not email:
        return _scim_error(400, "Missing or invalid email in userName or emails array")

    # Check for existing user. Match on email always; match on externalId only
    # when one was supplied — otherwise `external_id == None` compiles to
    # `IS NULL` and would collide with every user that has no external_id.
    ext_id = body.get("externalId")
    dup_conditions = [User.email == email]
    if ext_id:
        dup_conditions.append(User.external_id == ext_id)
    existing = (await db.execute(
        select(User).where(or_(*dup_conditions))
    )).scalars().first()
    if existing:
        return _scim_error(409, f"User with email {email} already exists", "uniqueness")

    display_name = (
        body.get("displayName")
        or (body.get("name") or {}).get("formatted")
        or email.split("@")[0]
    )

    user = User(
        id=str(uuid.uuid4()),
        external_id=body.get("externalId"),
        email=email,
        display_name=display_name,
        avatar_url=body.get("photos", [{}])[0].get("value") if body.get("photos") else None,
        is_active=_coerce_bool(body.get("active", True)),
    )
    db.add(user)

    # Enterprise user extension (Entra/Okta). No sub-attribute maps to a real
    # User column, so the whole extension is accepted and round-tripped.
    ext = body.get(ENTERPRISE_USER_SCHEMA)
    if isinstance(ext, dict):
        _set_enterprise_ext(user, ext)

    # Audit log
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_user",
        resource_id=str(user.id),
        action="created",
        after={"email": email, "external_id": body.get("externalId")},
    ))

    await db.flush()
    base_url = str(request.base_url).rstrip("/")
    return _scim_response(_user_to_scim(user, base_url), status_code=201)


@scim_router.get("/Users/{user_id}")
async def get_user(
    user_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Get a single user by ID."""
    identity.assert_permission("users:read")

    user = await db.get(User, user_id)
    if user is None:
        return _scim_error(404, f"User {user_id} not found")

    base_url = str(request.base_url).rstrip("/")
    return _scim_response(_user_to_scim(user, base_url))


@scim_router.put("/Users/{user_id}")
async def replace_user(
    user_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Full replacement of a user resource (SCIM PUT)."""
    identity.assert_permission("users:write")

    user = await db.get(User, user_id)
    if user is None:
        return _scim_error(404, f"User {user_id} not found")

    body = await request.json()

    email = _extract_email(body)
    if email:
        user.email = email
    if body.get("displayName"):
        user.display_name = body["displayName"]
    elif body.get("name", {}).get("formatted"):
        user.display_name = body["name"]["formatted"]
    if body.get("externalId") is not None:
        user.external_id = body["externalId"]
    if "active" in body:
        user.is_active = _coerce_bool(body["active"])

    # Enterprise extension: PUT is a full replace, but we only overwrite the
    # stored extension when the payload actually carries it (an IdP that never
    # sends the extension should not have it wiped on every sync).
    ext = body.get(ENTERPRISE_USER_SCHEMA)
    if isinstance(ext, dict):
        _set_enterprise_ext(user, ext)

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_user",
        resource_id=str(user.id),
        action="updated",
        after={"email": user.email, "active": user.is_active},
    ))

    await db.flush()
    # Reload within the async context so server-side onupdate columns
    # (updated_at) are populated before the synchronous serializer reads them —
    # otherwise a lazy refresh fires outside the greenlet on aiosqlite.
    await db.refresh(user)
    base_url = str(request.base_url).rstrip("/")
    return _scim_response(_user_to_scim(user, base_url))


@scim_router.patch("/Users/{user_id}")
async def patch_user(
    user_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Partial update of a user (SCIM PATCH with Operations)."""
    identity.assert_permission("users:write")

    user = await db.get(User, user_id)
    if user is None:
        return _scim_error(404, f"User {user_id} not found")

    body = await request.json()
    operations = body.get("Operations", [])

    for op in operations:
        op_type = op.get("op", "").lower()
        raw_path = op.get("path", "") or ""
        path = raw_path.lower()
        value = op.get("value")

        # ── Enterprise extension paths (URNs are case-sensitive) ─────────────
        # e.g. path = "urn:...:enterprise:2.0:User:department" or
        #      path = "urn:...:enterprise:2.0:User" with a dict value.
        if raw_path.startswith(ENTERPRISE_USER_SCHEMA):
            sub = raw_path[len(ENTERPRISE_USER_SCHEMA):].lstrip(":")
            if op_type in ("add", "replace"):
                _merge_enterprise_ext_path(user, sub, value)
            elif op_type == "remove":
                _remove_enterprise_ext_path(user, sub)
            continue

        if op_type in ("add", "replace"):
            # For single-valued attributes SCIM 'add' and 'replace' are
            # equivalent (RFC 7644 §3.5.2.1/§3.5.2.3).
            if path == "active":
                user.is_active = _coerce_bool(value)
            elif path in ("username", "username.value"):
                user.email = value
            elif path == "displayname":
                user.display_name = value
            elif path == "externalid":
                user.external_id = value
            elif path == "name.formatted":
                user.display_name = value
            elif not path and isinstance(value, dict):
                # Path-less op: value is a dict of attributes, possibly
                # including the enterprise extension keyed by its URN.
                if "active" in value:
                    user.is_active = _coerce_bool(value["active"])
                if "userName" in value:
                    user.email = value["userName"]
                if "displayName" in value:
                    user.display_name = value["displayName"]
                if "externalId" in value:
                    user.external_id = value["externalId"]
                if isinstance(value.get(ENTERPRISE_USER_SCHEMA), dict):
                    _merge_enterprise_ext(user, value[ENTERPRISE_USER_SCHEMA])
        elif op_type == "remove":
            # Clear nullable attributes. userName/displayName are NOT NULL in
            # the schema, so a remove on those is ignored rather than erroring.
            if path == "externalid":
                user.external_id = None
            elif path == "active":
                user.is_active = False

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_user",
        resource_id=str(user.id),
        action="patched",
        after={"active": user.is_active, "email": user.email},
    ))

    if not user.is_active:
        clear_permissions_cache(str(user.id))

    await db.flush()
    # See replace_user: reload before the sync serializer to avoid an
    # out-of-greenlet lazy refresh of server-onupdate columns on aiosqlite.
    await db.refresh(user)
    base_url = str(request.base_url).rstrip("/")
    return _scim_response(_user_to_scim(user, base_url))


@scim_router.delete("/Users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Deactivate a user (SCIM DELETE = soft-delete / deactivation)."""
    identity.assert_permission("users:delete")

    user = await db.get(User, user_id)
    if user is None:
        return _scim_error(404, f"User {user_id} not found")

    user.is_active = False
    clear_permissions_cache(str(user.id))

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_user",
        resource_id=str(user.id),
        action="deactivated",
    ))

    await db.flush()
    return JSONResponse(status_code=204, content=None)


# ── Group endpoints (mapped to RBAC roles) ──────────────────────────────────


@scim_router.get("/Groups")
async def list_groups(
    request: Request,
    filter: Optional[str] = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=1, le=200),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """List or filter groups (SCIM ListResponse)."""
    identity.assert_permission("roles:read")

    filter_expr = None
    if filter:
        try:
            filter_expr = _build_group_filter(filter)
        except ScimFilterError as exc:
            return _scim_error(400, str(exc), "invalidFilter")

    q = select(RbacRole).order_by(RbacRole.created_at)
    if filter_expr is not None:
        q = q.where(filter_expr)

    # totalResults reflects the FILTERED set (see list_users).
    count_q = select(func.count()).select_from(RbacRole)
    if filter_expr is not None:
        count_q = count_q.where(filter_expr)
    total = await db.scalar(count_q) or 0

    offset = max(0, startIndex - 1)
    q = q.offset(offset).limit(count)

    result = await db.execute(q)
    roles = result.scalars().all()

    base_url = str(request.base_url).rstrip("/")
    return _scim_response({
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": total,
        "startIndex": startIndex,
        "itemsPerPage": len(roles),
        "Resources": [_role_to_scim_group(r, base_url=base_url) for r in roles],
    })


@scim_router.post("/Groups")
async def create_group(
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Create a group (custom RBAC role) via SCIM provisioning."""
    identity.assert_permission("roles:write")
    body = await request.json()

    display_name = body.get("displayName", "").strip()
    if not display_name:
        return _scim_error(400, "displayName is required")

    # Check uniqueness
    existing = (await db.execute(
        select(RbacRole).where(RbacRole.name == display_name)
    )).scalars().first()
    if existing:
        return _scim_error(409, f"Group {display_name} already exists", "uniqueness")

    role = RbacRole(
        id=str(uuid.uuid4()),
        name=display_name,
        description=f"SCIM-provisioned role: {display_name}",
        allow=[],
        deny=[],
        is_system=False,
    )
    db.add(role)

    # Process member assignments
    members = body.get("members", [])
    for member in members:
        member_id = member.get("value")
        if member_id:
            assignment = RbacAssignment(
                id=str(uuid.uuid4()),
                user_id=member_id,
                role_id=str(role.id),
                assigned_by=identity.actor_id,
            )
            db.add(assignment)

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_group",
        resource_id=str(role.id),
        action="created",
        after={"name": display_name, "member_count": len(members)},
    ))

    await db.flush()
    base_url = str(request.base_url).rstrip("/")
    return _scim_response(
        _role_to_scim_group(role, base_url=base_url),
        status_code=201,
    )


@scim_router.get("/Groups/{group_id}")
async def get_group(
    group_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Get a single group by ID."""
    identity.assert_permission("roles:read")

    role = await db.get(RbacRole, group_id)
    if role is None:
        return _scim_error(404, f"Group {group_id} not found")

    # Fetch members
    assignments = (await db.execute(
        select(RbacAssignment).where(RbacAssignment.role_id == group_id)
    )).scalars().all()

    members = []
    for a in assignments:
        user = await db.get(User, a.user_id)
        members.append({
            "value": a.user_id,
            "display": user.display_name if user else a.user_id,
        })

    base_url = str(request.base_url).rstrip("/")
    return _scim_response(_role_to_scim_group(role, members=members, base_url=base_url))


@scim_router.put("/Groups/{group_id}")
async def replace_group(
    group_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Full replacement of a group resource (SCIM PUT)."""
    identity.assert_permission("roles:write")

    role = await db.get(RbacRole, group_id)
    if role is None:
        return _scim_error(404, f"Group {group_id} not found")

    if role.is_system:
        return _scim_error(400, "Cannot modify system roles via SCIM")

    body = await request.json()

    if body.get("displayName"):
        role.name = body["displayName"]

    # Replace member list: remove existing, add new
    await db.execute(
        select(RbacAssignment).where(RbacAssignment.role_id == group_id)
    )
    existing_assignments = (await db.execute(
        select(RbacAssignment).where(RbacAssignment.role_id == group_id)
    )).scalars().all()
    for a in existing_assignments:
        await db.delete(a)

    members = body.get("members", [])
    for member in members:
        member_id = member.get("value")
        if member_id:
            assignment = RbacAssignment(
                id=str(uuid.uuid4()),
                user_id=member_id,
                role_id=str(role.id),
                assigned_by=identity.actor_id,
            )
            db.add(assignment)

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_group",
        resource_id=str(role.id),
        action="replaced",
        after={"name": role.name, "member_count": len(members)},
    ))

    await db.flush()
    await db.refresh(role)  # see replace_user — avoid out-of-greenlet refresh
    clear_permissions_cache()  # Invalidate all caches since membership changed
    base_url = str(request.base_url).rstrip("/")
    return _scim_response(_role_to_scim_group(role, base_url=base_url))


@scim_router.patch("/Groups/{group_id}")
async def patch_group(
    group_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Partial update of a group (SCIM PATCH with Operations)."""
    identity.assert_permission("roles:write")

    role = await db.get(RbacRole, group_id)
    if role is None:
        return _scim_error(404, f"Group {group_id} not found")

    if role.is_system:
        return _scim_error(400, "Cannot modify system roles via SCIM")

    body = await request.json()
    operations = body.get("Operations", [])

    async def _add_member(member_id: str):
        if not member_id:
            return
        existing = (await db.execute(
            select(RbacAssignment).where(
                RbacAssignment.role_id == group_id,
                RbacAssignment.user_id == member_id,
            )
        )).scalars().first()
        if not existing:
            db.add(RbacAssignment(
                id=str(uuid.uuid4()),
                user_id=member_id,
                role_id=str(role.id),
                assigned_by=identity.actor_id,
            ))

    async def _remove_member(member_id: str):
        if not member_id:
            return
        assignment = (await db.execute(
            select(RbacAssignment).where(
                RbacAssignment.role_id == group_id,
                RbacAssignment.user_id == member_id,
            )
        )).scalars().first()
        if assignment:
            await db.delete(assignment)

    async def _clear_members():
        rows = (await db.execute(
            select(RbacAssignment).where(RbacAssignment.role_id == group_id)
        )).scalars().all()
        for a in rows:
            await db.delete(a)

    def _member_ids(val) -> list[str]:
        items = val if isinstance(val, list) else [val]
        out = []
        for m in items:
            mid = m.get("value") if isinstance(m, dict) else m
            if mid:
                out.append(mid)
        return out

    for op in operations:
        op_type = op.get("op", "").lower()
        raw_path = op.get("path", "") or ""
        path = raw_path.lower()
        value = op.get("value")

        if op_type == "replace" and path == "displayname":
            role.name = value
        elif op_type == "replace" and not path and isinstance(value, dict):
            # Path-less replace: value dict may carry displayName / members.
            if value.get("displayName"):
                role.name = value["displayName"]
            if "members" in value:
                await _clear_members()
                for mid in _member_ids(value.get("members")):
                    await _add_member(mid)
        elif op_type == "add" and path == "members":
            for mid in _member_ids(value):
                await _add_member(mid)
        elif op_type == "replace" and path == "members":
            # Full membership replacement.
            await _clear_members()
            for mid in _member_ids(value):
                await _add_member(mid)
        elif op_type == "remove" and path.startswith("members"):
            # Three shapes are accepted:
            #   members[value eq "id"]   → remove that one member
            #   path "members" + value   → remove the listed member(s)
            #   path "members" (no value)→ remove all members
            member_id = _extract_member_id_from_path(raw_path)
            if member_id:
                await _remove_member(member_id)
            elif value is not None:
                for mid in _member_ids(value):
                    await _remove_member(mid)
            else:
                await _clear_members()

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_group",
        resource_id=str(role.id),
        action="patched",
        after={"name": role.name, "operations": len(operations)},
    ))

    await db.flush()
    await db.refresh(role)  # see replace_user — avoid out-of-greenlet refresh
    clear_permissions_cache()
    base_url = str(request.base_url).rstrip("/")
    return _scim_response(_role_to_scim_group(role, base_url=base_url))


@scim_router.delete("/Groups/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Delete a group (RBAC role)."""
    identity.assert_permission("roles:delete")

    role = await db.get(RbacRole, group_id)
    if role is None:
        return _scim_error(404, f"Group {group_id} not found")

    if role.is_system:
        return _scim_error(400, "Cannot delete system roles via SCIM")

    # Remove all assignments first
    assignments = (await db.execute(
        select(RbacAssignment).where(RbacAssignment.role_id == group_id)
    )).scalars().all()
    for a in assignments:
        await db.delete(a)

    await db.delete(role)

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="scim_group",
        resource_id=group_id,
        action="deleted",
    ))

    await db.flush()
    clear_permissions_cache()
    return JSONResponse(status_code=204, content=None)


# ── SCIM filter parser (RFC 7644 §3.4.2.2) ──────────────────────────────────
#
# A real recursive-descent parser for the SCIM filter grammar. Supports the
# operators Okta and Microsoft Entra actually emit during provisioning:
#   eq ne co sw ew pr gt ge lt le, logical and/or/not, and parenthesised groups.
# Every attribute is resolved against an allow-list mapping to a real ORM
# column; unknown attributes and malformed filters raise ScimFilterError, which
# the list endpoints translate into a SCIM 400 (scimType "invalidFilter").
#
# Values are always bound as SQLAlchemy parameters (== / ilike bind params), so
# there is no string interpolation into SQL — injection-safe by construction.
# LIKE wildcards in user input are escaped for co/sw/ew so a literal '%' or '_'
# cannot widen the match.

_COMPARE_OPS = {"eq", "ne", "co", "sw", "ew", "gt", "ge", "lt", "le"}


class ScimFilterError(ValueError):
    """Raised for unsupported/malformed SCIM filters → surfaced as HTTP 400."""


def _coerce_bool(value) -> bool:
    """Coerce a SCIM scalar (bool / int / 'true'/'false' string) to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no", ""):
            return False
        raise ScimFilterError(f"Invalid boolean value: {value!r}")
    raise ScimFilterError(f"Invalid boolean value: {value!r}")


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters so co/sw/ew match literally (escape char \\)."""
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


# Attribute allow-lists: lowercased SCIM attribute path → (ORM column, type).
# type is "string" or "bool" and governs which operators are valid.
_USER_FILTER_ATTRS = {
    "username": (User.email, "string"),
    "externalid": (User.external_id, "string"),
    "active": (User.is_active, "bool"),
    "emails.value": (User.email, "string"),
    "emails": (User.email, "string"),
    "displayname": (User.display_name, "string"),
    "name.formatted": (User.display_name, "string"),
    "id": (User.id, "string"),
}

_GROUP_FILTER_ATTRS = {
    "displayname": (RbacRole.name, "string"),
    "id": (RbacRole.id, "string"),
}


def _tokenize_filter(s: str) -> list[tuple[str, Optional[str]]]:
    """Tokenize a SCIM filter into ('(' | ')' | 'STR' | 'WORD', value) tuples."""
    tokens: list[tuple[str, Optional[str]]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c in "()":
            tokens.append((c, None))
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n:
                ch = s[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(s[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    break
                buf.append(ch)
                i += 1
            if i >= n or s[i] != '"':
                raise ScimFilterError("Unterminated string literal in filter")
            i += 1
            tokens.append(("STR", "".join(buf)))
            continue
        # Bareword: attribute path, operator, logical keyword, or bare literal.
        buf = []
        while i < n and not s[i].isspace() and s[i] not in '()"':
            buf.append(s[i])
            i += 1
        tokens.append(("WORD", "".join(buf)))
    return tokens


class _FilterParser:
    """Recursive-descent parser: or > and > primary (attrExp | group | not)."""

    def __init__(self, tokens, attr_map):
        self.toks = tokens
        self.pos = 0
        self.attr_map = attr_map

    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def _advance(self):
        tok = self._peek()
        self.pos += 1
        return tok

    def parse(self):
        if not self.toks:
            raise ScimFilterError("Empty filter")
        expr = self._parse_or()
        if self.pos != len(self.toks):
            raise ScimFilterError("Unexpected trailing tokens in filter")
        return expr

    def _parse_or(self):
        left = self._parse_and()
        while True:
            typ, val = self._peek()
            if typ == "WORD" and val.lower() == "or":
                self._advance()
                left = or_(left, self._parse_and())
            else:
                return left

    def _parse_and(self):
        left = self._parse_primary()
        while True:
            typ, val = self._peek()
            if typ == "WORD" and val.lower() == "and":
                self._advance()
                left = and_(left, self._parse_primary())
            else:
                return left

    def _parse_primary(self):
        typ, val = self._peek()
        if typ == "(":
            self._advance()
            expr = self._parse_or()
            t2, _ = self._peek()
            if t2 != ")":
                raise ScimFilterError("Expected ')' in filter")
            self._advance()
            return expr
        if typ == "WORD" and val.lower() == "not":
            self._advance()
            t2, _ = self._peek()
            if t2 != "(":
                raise ScimFilterError("Expected '(' after 'not'")
            self._advance()
            expr = self._parse_or()
            t3, _ = self._peek()
            if t3 != ")":
                raise ScimFilterError("Expected ')' to close 'not(...)'")
            self._advance()
            return not_(expr)
        return self._parse_attr_exp()

    def _resolve(self, attr):
        key = attr.lower()
        if key not in self.attr_map:
            raise ScimFilterError(f"Unsupported filter attribute: {attr}")
        return self.attr_map[key]

    def _parse_attr_exp(self):
        typ, attr = self._advance()
        if typ != "WORD" or not attr:
            raise ScimFilterError("Expected an attribute name in filter")
        otyp, op = self._advance()
        if otyp != "WORD" or not op:
            raise ScimFilterError("Expected an operator in filter")
        op_l = op.lower()

        col, col_type = self._resolve(attr)

        if op_l == "pr":
            if col_type == "bool":
                return col.isnot(None)
            return and_(col.isnot(None), col != "")

        if op_l not in _COMPARE_OPS:
            raise ScimFilterError(f"Unsupported filter operator: {op}")

        vtyp, vval = self._advance()
        if vtyp is None:
            raise ScimFilterError(f"Expected a value after operator '{op}'")

        if col_type == "bool":
            if op_l not in ("eq", "ne"):
                raise ScimFilterError(
                    f"Operator '{op}' is not valid for boolean attribute '{attr}'"
                )
            bval = _coerce_bool(vval)
            return col == bval if op_l == "eq" else col != bval

        sval = str(vval)
        if op_l == "eq":
            return col == sval
        if op_l == "ne":
            return col != sval
        if op_l == "co":
            return col.ilike(f"%{_escape_like(sval)}%", escape="\\")
        if op_l == "sw":
            return col.ilike(f"{_escape_like(sval)}%", escape="\\")
        if op_l == "ew":
            return col.ilike(f"%{_escape_like(sval)}", escape="\\")
        if op_l == "gt":
            return col > sval
        if op_l == "ge":
            return col >= sval
        if op_l == "lt":
            return col < sval
        if op_l == "le":
            return col <= sval
        raise ScimFilterError(f"Unsupported filter operator: {op}")  # pragma: no cover


def _build_filter_expr(filter_str: str, attr_map: dict):
    """Parse a SCIM filter string into a SQLAlchemy boolean expression."""
    tokens = _tokenize_filter(filter_str.strip())
    return _FilterParser(tokens, attr_map).parse()


def _build_user_filter(filter_str: str):
    """Build a SQLAlchemy WHERE expression for a SCIM Users filter."""
    return _build_filter_expr(filter_str, _USER_FILTER_ATTRS)


def _build_group_filter(filter_str: str):
    """Build a SQLAlchemy WHERE expression for a SCIM Groups filter."""
    return _build_filter_expr(filter_str, _GROUP_FILTER_ATTRS)


def _extract_email(body: dict) -> Optional[str]:
    """Extract email from SCIM user body (userName or emails array)."""
    # Prefer userName (SCIM convention for email-as-username)
    username = body.get("userName")
    if username and "@" in username:
        return username

    # Fallback to emails array
    emails = body.get("emails", [])
    for e in emails:
        if isinstance(e, dict) and e.get("value"):
            return e["value"]
        elif isinstance(e, str):
            return e

    return username  # May be a non-email username


def _extract_member_id_from_path(path: str) -> Optional[str]:
    """Extract member ID from SCIM filter path like members[value eq "uuid"]."""
    import re
    m = re.search(r'value\s+eq\s+"([^"]+)"', path)
    return m.group(1) if m else None
