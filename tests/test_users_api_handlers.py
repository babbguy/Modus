"""Tests for orchestrator.api.users — all endpoint handlers."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    Invitation,
    RbacRole,
    Team,
    User,
)


def _user(email: str = "user@example.com", **kw) -> User:
    defaults = {
        "id": str(uuid.uuid4()),
        "email": email,
        "display_name": email.split("@")[0],
    }
    defaults.update(kw)
    return User(**defaults)


def _role(name: str = "viewer", **kw) -> RbacRole:
    defaults = {
        "id": str(uuid.uuid4()),
        "name": name,
        "allow": ["apps:read", "users:read"],
        "deny": [],
        "is_system": False,
    }
    defaults.update(kw)
    return RbacRole(**defaults)


@pytest_asyncio.fixture
async def seed_users(db_session: AsyncSession):
    u1 = _user("alice@example.com", display_name="Alice")
    u2 = _user("bob@example.com", display_name="Bob")
    role = _role("operator")
    team = Team(id=str(uuid.uuid4()), slug="usr-team", name="User Team")
    db_session.add_all([u1, u2, role, team])
    await db_session.commit()
    return {"alice": u1, "bob": u2, "role": role, "team": team}


# ── GET /users ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users(client, seed_users):
    resp = await client.get("/api/v1/users")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    emails = [u["email"] for u in data]
    assert "alice@example.com" in emails


@pytest.mark.asyncio
async def test_list_users_filter_active(client, seed_users):
    resp = await client.get("/api/v1/users?is_active=true")
    assert resp.status_code == 200
    assert all(u["is_active"] for u in resp.json())


@pytest.mark.asyncio
async def test_list_users_pagination(client, seed_users):
    resp = await client.get("/api/v1/users?limit=1&offset=0")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── GET /users/me ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_me_stub_auth(client):
    """In stub auth mode, /me returns identity-derived fields."""
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "platform_admin"


# ── PUT /users/me ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_me_stub(client):
    resp = await client.put("/api/v1/users/me", json={
        "display_name": "Jane Doe",
        "email": "jane@example.com",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Jane Doe"


# ── PUT /users/me/password ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_password_returns_501(client):
    resp = await client.put("/api/v1/users/me/password", json={
        "current_password": "old",
        "new_password": "new",
    })
    assert resp.status_code == 501


# ── POST /users/me/api-key ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_api_key_returns_501(client):
    resp = await client.post("/api/v1/users/me/api-key")
    assert resp.status_code == 501


# ── GET /users/{user_id} ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user(client, seed_users):
    uid = str(seed_users["alice"].id)
    resp = await client.get(f"/api/v1/users/{uid}")
    assert resp.status_code == 200
    assert resp.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_user_not_found(client):
    resp = await client.get(f"/api/v1/users/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── PATCH /users/{user_id} ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_user(client, seed_users):
    uid = str(seed_users["alice"].id)
    resp = await client.patch(f"/api/v1/users/{uid}", json={
        "display_name": "Alice Wonderland",
        "is_active": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Alice Wonderland"
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_update_user_not_found(client):
    resp = await client.patch(f"/api/v1/users/{uuid.uuid4()}", json={
        "display_name": "Ghost",
    })
    assert resp.status_code == 404


# ── DELETE /users/{user_id} ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user(client, seed_users):
    uid = str(seed_users["bob"].id)
    resp = await client.delete(f"/api/v1/users/{uid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_user_not_found(client):
    resp = await client.delete(f"/api/v1/users/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── GET /users/invite/channels ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_invite_channels(client):
    resp = await client.get("/api/v1/users/invite/channels")
    assert resp.status_code == 200
    data = resp.json()
    assert "all" in data
    assert "email" in data["all"]


# ── POST /users/invite ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_user(client, seed_users):
    role_id = str(seed_users["role"].id)
    resp = await client.post("/api/v1/users/invite", json={
        "email": "newguy@example.com",
        "display_name": "New Guy",
        "role_id": role_id,
        "channel": "email",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "newguy@example.com"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_invite_existing_user(client, seed_users):
    role_id = str(seed_users["role"].id)
    resp = await client.post("/api/v1/users/invite", json={
        "email": "alice@example.com",
        "display_name": "Alice",
        "role_id": role_id,
        "channel": "email",
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_invite_bad_role(client):
    resp = await client.post("/api/v1/users/invite", json={
        "email": "x@x.com",
        "display_name": "X",
        "role_id": str(uuid.uuid4()),
        "channel": "email",
    })
    assert resp.status_code == 404


# ── GET /users/invitations ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_invitations(client, seed_users):
    """/users/invitations is declared before /users/{user_id}, so it is not
    captured as a user id (it used to 404 on SQLite and 500 on PostgreSQL)."""
    resp = await client.get("/api/v1/users/invitations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_invitations_filter_status(client, seed_users):
    resp = await client.get("/api/v1/users/invitations?status=pending")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── POST /users/invitations/{id}/revoke ──────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_invitation(client, db_session, seed_users):
    import bcrypt
    token = "mds_invite_testtoken123456789012345678"
    token_hash = bcrypt.hashpw(token.encode(), bcrypt.gensalt(4)).decode()
    inv = Invitation(
        id=str(uuid.uuid4()),
        email="rev@example.com",
        role_id=str(seed_users["role"].id),
        invited_by=str(seed_users["alice"].id),
        token_hash=token_hash,
        token_prefix=token[:20],
        channel="email",
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(inv)
    await db_session.commit()

    resp = await client.post(f"/api/v1/users/invitations/{inv.id}/revoke")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_revoke_invitation_not_found(client):
    resp = await client.post(f"/api/v1/users/invitations/{uuid.uuid4()}/revoke")
    assert resp.status_code == 404


# ── POST /users/invitations/accept ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="SQLite stores naive datetimes; handler compares with tz-aware datetime",
    strict=False,
)
async def test_accept_invitation(client, db_session, seed_users):
    import bcrypt
    token = "mds_invite_realtoken12345678901234567X"
    token_hash = bcrypt.hashpw(token.encode(), bcrypt.gensalt(4)).decode()
    inv = Invitation(
        id=str(uuid.uuid4()),
        email="accepted@example.com",
        role_id=str(seed_users["role"].id),
        invited_by=str(seed_users["alice"].id),
        token_hash=token_hash,
        token_prefix=token[:20],
        channel="email",
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(inv)
    await db_session.commit()

    resp = await client.post("/api/v1/users/invitations/accept", json={
        "token": token,
        "display_name": "Accepted User",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "accepted@example.com"
    assert data["display_name"] == "Accepted User"


@pytest.mark.asyncio
async def test_accept_invitation_bad_token_format(client):
    resp = await client.post("/api/v1/users/invitations/accept", json={
        "token": "bad_token",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_accept_invitation_invalid_token(client):
    resp = await client.post("/api/v1/users/invitations/accept", json={
        "token": "mds_invite_nottherealtoken1234567890",
    })
    assert resp.status_code == 404


# ── GET /users/me/preferences ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_preferences_stub(client):
    """In stub mode, user_id is None so returns 400."""
    resp = await client.get("/api/v1/users/me/preferences")
    assert resp.status_code == 400


# ── PUT /users/me/preferences ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_preferences_stub(client):
    resp = await client.put("/api/v1/users/me/preferences", json={
        "theme": "dark",
    })
    assert resp.status_code == 400
