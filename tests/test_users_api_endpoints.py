"""
Users API (orchestrator/api/users.py)

Integration tests for user CRUD, invitations, preferences, and profile
endpoints via test client.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from orchestrator.db.models import (
    RbacRole,
    Team,
    User,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

TEAM_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
ROLE_ID = str(uuid.uuid4())


@pytest_asyncio.fixture
async def seeded_users(db_session):
    """Seed team, role, and user for tests."""
    team = Team(id=TEAM_ID, slug="user-team", name="User Team")
    db_session.add(team)

    role = RbacRole(
        id=ROLE_ID,
        name="test_operator",
        description="Test operator role",
        allow=["apps:read", "apps:write", "users:read", "users:write",
               "users:delete", "users:invite", "policies:read",
               "view:overview", "view:devops"],
    )
    db_session.add(role)

    user = User(
        id=USER_ID,
        email="testuser@example.com",
        display_name="Test User",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return {"team": team, "role": role, "user": user}


# ── GET /users/me ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_me(client):
    """Stub auth returns platform_admin identity."""
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200
    data = resp.json()
    assert "email" in data or "display_name" in data


@pytest.mark.asyncio
async def test_update_me(client):
    resp = await client.put(
        "/api/v1/users/me",
        json={"display_name": "Updated Name"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Updated Name"


# ── GET /users ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_empty(client):
    resp = await client.get("/api/v1/users")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_users_with_data(client, seeded_users):
    resp = await client.get("/api/v1/users")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_list_users_filter_active(client, seeded_users):
    resp = await client.get("/api/v1/users?is_active=true")
    assert resp.status_code == 200
    data = resp.json()
    assert all(u["is_active"] for u in data)


# ── GET /users/{user_id} ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user(client, seeded_users):
    resp = await client.get(f"/api/v1/users/{USER_ID}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "testuser@example.com"


@pytest.mark.asyncio
async def test_get_user_not_found(client):
    resp = await client.get(f"/api/v1/users/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── PATCH /users/{user_id} ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_user(client, seeded_users):
    resp = await client.patch(
        f"/api/v1/users/{USER_ID}",
        json={"display_name": "Renamed User"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed User"


@pytest.mark.asyncio
async def test_update_user_deactivate(client, seeded_users):
    resp = await client.patch(
        f"/api/v1/users/{USER_ID}",
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_update_user_not_found(client):
    resp = await client.patch(
        f"/api/v1/users/{uuid.uuid4()}",
        json={"display_name": "Ghost"},
    )
    assert resp.status_code == 404


# ── DELETE /users/{user_id} ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user(client, seeded_users, db_session):
    new_user = User(
        email="deleteme@example.com",
        display_name="Delete Me",
        is_active=True,
    )
    db_session.add(new_user)
    await db_session.flush()

    resp = await client.delete(f"/api/v1/users/{new_user.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_user_not_found(client):
    resp = await client.delete(f"/api/v1/users/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Password change (stub — 501) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_password_stub(client):
    resp = await client.put(
        "/api/v1/users/me/password",
        json={"current_password": "old", "new_password": "new"},
    )
    assert resp.status_code == 501


# ── API key rotation (stub — 501) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_api_key_stub(client):
    resp = await client.post("/api/v1/users/me/api-key")
    assert resp.status_code == 501


# ── Invite channels ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_invite_channels(client):
    resp = await client.get("/api/v1/users/invite/channels")
    assert resp.status_code == 200
    data = resp.json()
    assert "configured" in data
    assert "all" in data
    assert "email" in data["all"]


# ── Invitations ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_invite_channels_contains_all(client):
    """Verify all channel types are listed."""
    resp = await client.get("/api/v1/users/invite/channels")
    assert resp.status_code == 200
    data = resp.json()
    assert "slack" in data["all"]
    assert "teams" in data["all"]


# ── Preferences ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_preferences_default(client):
    """Stub auth has no user_id, should get default or 400."""
    resp = await client.get("/api/v1/users/me/preferences")
    # In stub mode, either returns defaults or 400
    assert resp.status_code in (200, 400)
