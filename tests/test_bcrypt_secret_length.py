# Copyright 2026 babbguy
# SPDX-License-Identifier: FSL-1.1-ALv2
"""
Tests -- a presented secret longer than bcrypt can check is a wrong secret.

bcrypt reads at most 72 bytes. bcrypt 4 silently truncated longer input;
bcrypt 5 raises ValueError instead. Every key and token Modus issues is
shorter than 72 bytes, so a longer one presented with a real secret's lookup
prefix must be refused exactly like any other wrong secret, never with a 500.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from pydantic import ValidationError

from orchestrator.core.config import Settings, settings
from orchestrator.core.secret_hash import BCRYPT_MAX_BYTES, secret_matches
from orchestrator.db.models import Invitation, RbacRole, User

MASTER = {"X-Modus-APIKey": settings.master_api_key}
TOO_LONG = "x" * BCRYPT_MAX_BYTES  # appended to a real secret, keeping its prefix


def _hash(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt(4)).decode()


def test_secret_matches_checks_secrets_up_to_the_limit():
    secret = "mds_" + "a" * 32
    assert secret_matches(secret, _hash(secret))
    assert not secret_matches(secret + "b", _hash(secret))
    at_limit = "a" * BCRYPT_MAX_BYTES
    assert secret_matches(at_limit, _hash(at_limit))


def test_secret_matches_refuses_a_secret_over_the_limit():
    at_limit = "a" * BCRYPT_MAX_BYTES
    # bcrypt 4 truncated this to its first 72 bytes and accepted it.
    assert not secret_matches(at_limit + "a", _hash(at_limit))


def test_the_limit_counts_bytes_not_characters():
    two_byte = "é" * (BCRYPT_MAX_BYTES // 2)  # 72 bytes
    assert secret_matches(two_byte, _hash(two_byte))
    assert not secret_matches(two_byte + "é", _hash(two_byte))


def test_api_key_prefix_must_leave_room_for_the_key_under_the_limit():
    Settings(api_key_prefix="p" * 40)
    with pytest.raises(ValidationError) as exc:
        Settings(api_key_prefix="p" * 41)
    assert exc.value.errors()[0]["loc"] == ("api_key_prefix",)


async def test_ingest_refuses_an_over_long_key_with_a_real_prefix(client):
    reg = await client.post(
        "/api/v1/apps/register",
        json={"app_id": "long-key", "app_name": "long-key", "team_slug": "long-team"},
        headers=MASTER,
    )
    assert reg.status_code == 201, reg.text
    key = reg.json()["api_key"]

    resp = await client.post(
        "/api/v1/ingest",
        json={"batch_id": str(uuid.uuid4()), "records": [{
            "provider": "openai", "resource_type": "llm", "model": "gpt-4o",
            "input_tokens": 1, "output_tokens": 1, "total_cost": "0.00000100",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]},
        headers={"X-Modus-APIKey": key + TOO_LONG},
    )
    assert resp.status_code == 401, resp.text


async def test_self_register_refuses_an_over_long_team_token(client, caplog):
    team = await client.post("/api/v1/teams", json={"slug": "long-tok", "name": "Long"})
    assert team.status_code == 201, team.text
    gen = await client.post(f"/api/v1/teams/{team.json()['id']}/registration-token", headers=MASTER)
    assert gen.status_code == 201, gen.text
    token = gen.json()["registration_token"]

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        resp = await client.post(
            "/api/v1/self-register",
            json={"app_id": "long-tok-app", "app_name": "long-tok-app"},
            headers={"X-Modus-TeamToken": token + TOO_LONG},
        )
    assert resp.status_code == 401, resp.text
    # A wrong token is not a malformed stored hash.
    assert "Registration token check failed" not in caplog.text


async def test_accepting_an_over_long_invitation_token_is_not_found(client, db_session):
    inviter = User(id=str(uuid.uuid4()), email="inviter@example.com", display_name="Inviter")
    role = RbacRole(id=str(uuid.uuid4()), name="long-token-role", allow=["view:overview"], deny=[], is_system=False)
    token = "mds_invite_" + "t" * 43
    db_session.add_all([inviter, role, Invitation(
        id=str(uuid.uuid4()),
        email="invitee@example.com",
        role_id=role.id,
        invited_by=inviter.id,
        token_hash=_hash(token),
        token_prefix=token[:20],
        channel="email",
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )])
    await db_session.commit()

    resp = await client.post("/api/v1/users/invitations/accept", json={"token": token + TOO_LONG})
    assert resp.status_code == 404, resp.text
