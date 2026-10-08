# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""
Tests -- self-registration does not spend bcrypt CPU it does not need.

Every POST /api/v1/self-register hashed a fresh stable key at bcrypt cost 12
(about 0.3-0.6 s of CPU), reconnects included, although the key is discarded
and the agent authenticates with a session token. On a 1-CPU server a few apps
starting at once pushed registration past the SDK's 3 s timeout, and the SDK
then disables governance for that process (seen by the release gate's
concurrent-registration check).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from orchestrator.api import topology
from orchestrator.core.config import get_settings
from orchestrator.db.models import App

pytestmark = pytest.mark.asyncio


async def _token(client) -> tuple[str, str]:
    t = await client.post("/api/v1/teams", json={"slug": "cpu-team", "name": "CPU"})
    assert t.status_code == 201, t.text
    gen = await client.post(f"/api/v1/teams/{t.json()['id']}/registration-token",
                            headers={"X-Modus-APIKey": get_settings().master_api_key})
    assert gen.status_code == 201, gen.text
    return t.json()["id"], gen.json()["registration_token"]


def _spy(monkeypatch) -> list:
    calls: list = []
    real = topology._hash_key

    def spy(key, rounds=None):
        calls.append(rounds)
        return real(key, rounds) if rounds is not None else real(key)
    monkeypatch.setattr(topology, "_hash_key", spy)
    return calls


async def _register(client, token: str, app_id: str):
    return await client.post("/api/v1/self-register", json={"app_id": app_id, "app_name": app_id},
                             headers={"X-Modus-TeamToken": token})


async def test_reconnect_does_not_hash_a_key(client, monkeypatch):
    _, token = await _token(client)
    assert (await _register(client, token, "cpu-app")).status_code == 200
    calls = _spy(monkeypatch)
    r = await _register(client, token, "cpu-app")
    assert r.status_code == 200 and r.json()["registered"] is False
    assert calls == []


async def test_new_app_key_is_hashed_once_at_minimum_cost(client, db_session, monkeypatch):
    _, token = await _token(client)
    calls = _spy(monkeypatch)
    r = await _register(client, token, "cpu-new")
    assert r.status_code == 200 and r.json()["registered"] is True
    assert calls == [topology._DISCARDED_KEY_BCRYPT_ROUNDS] == [4]
    row = (await db_session.execute(select(App).where(App.app_id == "cpu-new"))).scalar_one()
    assert row.api_key_hash.startswith("$2b$04$")
    # The agent still gets a session token, never the stable key.
    assert r.json()["api_key"].startswith("mst_")
