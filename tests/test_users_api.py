"""
Tests for orchestrator.api.users — User CRUD endpoints, preferences, and
schema validation.
"""
from __future__ import annotations

import pytest

from orchestrator.api.users import (
    UserUpdateRequest,
    InviteRequest,
    AcceptInvitationRequest,
    PreferencesResponse,
    PreferencesUpdateRequest,
    ChannelsResponse,
    MeResponse,
    PasswordChangeRequest,
)
# ── Schema validation ────────────────────────────────────────────────────────


class TestSchemas:
    def test_user_update_all_none(self):
        req = UserUpdateRequest()
        assert req.display_name is None
        assert req.is_active is None
        assert req.avatar_url is None

    def test_user_update_partial(self):
        req = UserUpdateRequest(display_name="New Name")
        assert req.display_name == "New Name"
        assert req.is_active is None

    def test_invite_request_valid(self):
        req = InviteRequest(
            email="user@example.com",
            display_name="Test User",
            role_id="role-1",
            channel="email",
        )
        assert req.email == "user@example.com"
        assert req.channel == "email"

    def test_invite_request_invalid_email(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            InviteRequest(email="not-an-email", display_name="Test", role_id="r1")

    def test_invite_request_invalid_channel(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            InviteRequest(
                email="user@example.com",
                display_name="Test",
                role_id="r1",
                channel="sms",
            )

    def test_accept_invitation_request(self):
        req = AcceptInvitationRequest(token="mds_invite_xxx")
        assert req.token == "mds_invite_xxx"
        assert req.display_name is None

    def test_preferences_response_defaults(self):
        resp = PreferencesResponse()
        assert resp.default_team_id is None
        assert resp.timezone == "UTC"
        assert resp.theme == "system"
        assert resp.pinned_app_ids == []

    def test_preferences_update_valid_view(self):
        req = PreferencesUpdateRequest(default_view="executive")
        assert req.default_view == "executive"

    def test_preferences_update_invalid_view(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PreferencesUpdateRequest(default_view="nonexistent")

    def test_preferences_update_valid_theme(self):
        for theme in ("light", "dark", "system"):
            req = PreferencesUpdateRequest(theme=theme)
            assert req.theme == theme

    def test_preferences_update_invalid_theme(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PreferencesUpdateRequest(theme="neon")

    def test_channels_response(self):
        resp = ChannelsResponse(configured=["email"], all=["email", "slack", "teams"])
        assert len(resp.configured) == 1
        assert len(resp.all) == 3

    def test_me_response_defaults(self):
        resp = MeResponse()
        assert resp.id is None
        assert resp.team_ids == []

    def test_password_change_request(self):
        req = PasswordChangeRequest(current_password="old", new_password="new")
        assert req.current_password == "old"


# ── API integration tests ────────────────────────────────────────────────────


async def test_list_users_empty(client):
    resp = await client.get("/api/v1/users")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_users_pagination(client):
    resp = await client.get("/api/v1/users", params={"limit": 10, "offset": 0})
    assert resp.status_code == 200


async def test_list_users_active_filter(client):
    resp = await client.get("/api/v1/users", params={"is_active": True})
    assert resp.status_code == 200


async def test_get_me(client):
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200
    data = resp.json()
    # In stub mode, should return identity-based response
    assert "email" in data or "display_name" in data


async def test_update_me(client):
    resp = await client.put("/api/v1/users/me", json={
        "display_name": "Updated Name",
    })
    assert resp.status_code == 200


async def test_get_nonexistent_user(client):
    resp = await client.get("/api/v1/users/00000000-0000-0000-0000-000000000999")
    assert resp.status_code == 404


async def test_update_nonexistent_user(client):
    resp = await client.patch(
        "/api/v1/users/00000000-0000-0000-0000-000000000999",
        json={"display_name": "Ghost"},
    )
    assert resp.status_code == 404


async def test_delete_nonexistent_user(client):
    resp = await client.delete("/api/v1/users/00000000-0000-0000-0000-000000000999")
    assert resp.status_code == 404


async def test_change_password_returns_501(client):
    resp = await client.put("/api/v1/users/me/password", json={
        "current_password": "old",
        "new_password": "new",
    })
    assert resp.status_code == 501


async def test_rotate_api_key_returns_501(client):
    resp = await client.post("/api/v1/users/me/api-key")
    assert resp.status_code == 501


async def test_list_invite_channels(client):
    resp = await client.get("/api/v1/users/invite/channels")
    assert resp.status_code == 200
    data = resp.json()
    assert "configured" in data
    assert "all" in data
    assert set(data["all"]) == {"email", "slack", "teams"}
