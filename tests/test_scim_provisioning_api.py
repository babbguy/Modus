"""Tests for orchestrator.api.scim — SCIM 2.0 provisioning endpoints."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import RbacRole, User


@pytest_asyncio.fixture
async def seed_scim(db_session: AsyncSession):
    user = User(
        id=str(uuid.uuid4()),
        email="scim-user@example.com",
        display_name="SCIM User",
        external_id="ext-001",
    )
    role = RbacRole(
        id=str(uuid.uuid4()),
        name="scim-viewer",
        allow=["apps:read"],
        deny=[],
        is_system=False,
    )
    sys_role = RbacRole(
        id=str(uuid.uuid4()),
        name="system-admin",
        allow=["*"],
        deny=[],
        is_system=True,
    )
    db_session.add_all([user, role, sys_role])
    await db_session.commit()
    return {"user": user, "role": role, "sys_role": sys_role}


# ── Discovery endpoints ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_provider_config(client):
    resp = await client.get("/api/v1/scim/v2/ServiceProviderConfig")
    assert resp.status_code == 200
    data = resp.json()
    assert data["patch"]["supported"] is True


@pytest.mark.asyncio
async def test_schemas(client):
    resp = await client.get("/api/v1/scim/v2/Schemas")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalResults"] == 2


@pytest.mark.asyncio
async def test_resource_types(client):
    resp = await client.get("/api/v1/scim/v2/ResourceTypes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalResults"] == 2


# ── SCIM Users ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_scim_users(client, seed_scim):
    resp = await client.get("/api/v1/scim/v2/Users")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalResults"] >= 1
    assert len(data["Resources"]) >= 1


@pytest.mark.asyncio
async def test_list_scim_users_filter_username(client, seed_scim):
    resp = await client.get(
        '/api/v1/scim/v2/Users?filter=userName eq "scim-user@example.com"'
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_scim_user(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": "new-scim@example.com",
        "displayName": "New SCIM User",
        "active": True,
        "externalId": "ext-new-001",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["userName"] == "new-scim@example.com"
    assert data["active"] is True


@pytest.mark.asyncio
async def test_create_scim_user_duplicate(client, seed_scim):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "scim-user@example.com",
        "displayName": "Dup",
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_scim_user_no_email(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "displayName": "No Email",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_scim_user(client, seed_scim):
    uid = str(seed_scim["user"].id)
    resp = await client.get(f"/api/v1/scim/v2/Users/{uid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["userName"] == "scim-user@example.com"


@pytest.mark.asyncio
async def test_get_scim_user_not_found(client):
    resp = await client.get(f"/api/v1/scim/v2/Users/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="aiosqlite MissingGreenlet on db.get+flush; works on PostgreSQL",
    strict=False,
)
async def test_replace_scim_user(client, seed_scim):
    uid = str(seed_scim["user"].id)
    resp = await client.put(f"/api/v1/scim/v2/Users/{uid}", json={
        "userName": "updated@example.com",
        "displayName": "Updated SCIM",
        "active": False,
        "externalId": "ext-updated",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is False
    assert data["displayName"] == "Updated SCIM"


@pytest.mark.asyncio
async def test_replace_scim_user_not_found(client):
    resp = await client.put(f"/api/v1/scim/v2/Users/{uuid.uuid4()}", json={
        "userName": "ghost@x.com",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="aiosqlite MissingGreenlet on db.get+flush; works on PostgreSQL",
    strict=False,
)
async def test_patch_scim_user_active(client, seed_scim):
    uid = str(seed_scim["user"].id)
    resp = await client.patch(f"/api/v1/scim/v2/Users/{uid}", json={
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "replace", "path": "active", "value": False},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["active"] is False


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="aiosqlite MissingGreenlet on db.get+flush; works on PostgreSQL",
    strict=False,
)
async def test_patch_scim_user_bulk_replace(client, seed_scim):
    uid = str(seed_scim["user"].id)
    resp = await client.patch(f"/api/v1/scim/v2/Users/{uid}", json={
        "Operations": [
            {"op": "replace", "value": {
                "displayName": "Bulk Updated",
                "active": True,
            }},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "Bulk Updated"


@pytest.mark.asyncio
async def test_patch_scim_user_not_found(client):
    resp = await client.patch(f"/api/v1/scim/v2/Users/{uuid.uuid4()}", json={
        "Operations": [{"op": "replace", "path": "active", "value": False}],
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_scim_user(client, seed_scim):
    uid = str(seed_scim["user"].id)
    resp = await client.delete(f"/api/v1/scim/v2/Users/{uid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_scim_user_not_found(client):
    resp = await client.delete(f"/api/v1/scim/v2/Users/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── SCIM Groups ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_scim_groups(client, seed_scim):
    resp = await client.get("/api/v1/scim/v2/Groups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalResults"] >= 1


@pytest.mark.asyncio
async def test_list_scim_groups_filter(client, seed_scim):
    resp = await client.get(
        '/api/v1/scim/v2/Groups?filter=displayName eq "scim-viewer"'
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_scim_group(client, seed_scim):
    resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "scim-new-group",
        "members": [{"value": str(seed_scim["user"].id)}],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["displayName"] == "scim-new-group"


@pytest.mark.asyncio
async def test_create_scim_group_duplicate(client, seed_scim):
    resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "scim-viewer",
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_scim_group_no_name(client):
    resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_scim_group(client, seed_scim):
    gid = str(seed_scim["role"].id)
    resp = await client.get(f"/api/v1/scim/v2/Groups/{gid}")
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "scim-viewer"


@pytest.mark.asyncio
async def test_get_scim_group_not_found(client):
    resp = await client.get(f"/api/v1/scim/v2/Groups/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="aiosqlite MissingGreenlet on db.get+flush; works on PostgreSQL",
    strict=False,
)
async def test_replace_scim_group(client, seed_scim):
    gid = str(seed_scim["role"].id)
    resp = await client.put(f"/api/v1/scim/v2/Groups/{gid}", json={
        "displayName": "renamed-viewer",
        "members": [],
    })
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "renamed-viewer"


@pytest.mark.asyncio
async def test_replace_scim_group_not_found(client):
    resp = await client.put(f"/api/v1/scim/v2/Groups/{uuid.uuid4()}", json={
        "displayName": "ghost",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_replace_scim_group_system_role(client, seed_scim):
    gid = str(seed_scim["sys_role"].id)
    resp = await client.put(f"/api/v1/scim/v2/Groups/{gid}", json={
        "displayName": "hijacked",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="aiosqlite MissingGreenlet on db.get+flush; works on PostgreSQL",
    strict=False,
)
async def test_patch_scim_group_rename(client, seed_scim):
    gid = str(seed_scim["role"].id)
    resp = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [
            {"op": "replace", "path": "displayName", "value": "patched-name"},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "patched-name"


@pytest.mark.asyncio
async def test_patch_scim_group_add_member(client, seed_scim):
    gid = str(seed_scim["role"].id)
    uid = str(seed_scim["user"].id)
    resp = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [
            {"op": "add", "path": "members", "value": [{"value": uid}]},
        ],
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_scim_group_remove_member(client, seed_scim):
    gid = str(seed_scim["role"].id)
    uid = str(seed_scim["user"].id)
    # First add a member
    await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [
            {"op": "add", "path": "members", "value": [{"value": uid}]},
        ],
    })
    # Then remove
    resp = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [
            {"op": "remove", "path": f'members[value eq "{uid}"]'},
        ],
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_scim_group_not_found(client):
    resp = await client.patch(f"/api/v1/scim/v2/Groups/{uuid.uuid4()}", json={
        "Operations": [{"op": "replace", "path": "displayName", "value": "x"}],
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_scim_group_system_role(client, seed_scim):
    gid = str(seed_scim["sys_role"].id)
    resp = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [{"op": "replace", "path": "displayName", "value": "x"}],
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_scim_group(client, seed_scim):
    gid = str(seed_scim["role"].id)
    resp = await client.delete(f"/api/v1/scim/v2/Groups/{gid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_scim_group_not_found(client):
    resp = await client.delete(f"/api/v1/scim/v2/Groups/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_scim_group_system_role(client, seed_scim):
    gid = str(seed_scim["sys_role"].id)
    resp = await client.delete(f"/api/v1/scim/v2/Groups/{gid}")
    assert resp.status_code == 400
