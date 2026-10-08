# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""
Tests -- POST /api/v1/apps/register commits the app before it responds.

Registration used to hand the INSERT to the background write queue and answer
201 straight away, so the new app was missing from GET /apps on the next
request (the release gate's write-then-read check caught it on PostgreSQL),
and a write that failed after the response, such as an app_id reused after a
soft delete, left the caller holding a key for an app that never existed.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from orchestrator.core.config import settings
from orchestrator.db.models import App, AuditLog, Team

MASTER = {"X-Modus-APIKey": settings.master_api_key}


async def _register(client, app_id: str, team_slug: str = "reg-team"):
    return await client.post(
        "/api/v1/apps/register",
        json={"app_id": app_id, "app_name": app_id, "team_slug": team_slug},
        headers=MASTER,
    )


@pytest.mark.asyncio
async def test_registered_app_is_listed_on_the_next_request(client):
    r = await _register(client, "reg-listed")
    assert r.status_code == 201, r.text

    teams = (await client.get("/api/v1/teams")).json()
    team_id = next(t["id"] for t in teams if t["slug"] == "reg-team")
    apps = (await client.get(f"/api/v1/apps?team_id={team_id}")).json()
    assert "reg-listed" in {a["app_id"] for a in apps}


@pytest.mark.asyncio
async def test_registration_writes_the_app_row_and_audit_entry(client, db_session):
    r = await _register(client, "reg-row")
    assert r.status_code == 201, r.text

    app = (await db_session.execute(select(App).where(App.app_id == "reg-row"))).scalar_one()
    assert app.api_key_prefix == r.json()["api_key"][:16]
    audit = (await db_session.execute(
        select(AuditLog).where(AuditLog.resource_id == str(app.id), AuditLog.action == "registered")
    )).scalar_one()
    assert audit.actor_id == "master-key"


@pytest.mark.asyncio
async def test_new_key_works_immediately(client):
    r = await _register(client, "reg-key")
    assert r.status_code == 201, r.text
    ev = await client.post(
        "/api/v1/policy/evaluate",
        json={"provider": "openai", "model": "gpt-4o"},
        headers={"X-Modus-APIKey": r.json()["api_key"]},
    )
    assert ev.status_code == 200, ev.text
    assert ev.json()["decision"] == "allow"


@pytest.mark.asyncio
async def test_duplicate_registration_is_rejected(client):
    assert (await _register(client, "reg-dup")).status_code == 201
    second = await _register(client, "reg-dup")
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_reusing_a_soft_deleted_app_id_is_a_409_not_a_dead_key(client, db_session):
    team = Team(slug="reg-deleted-team", name="Reg Deleted")
    db_session.add(team)
    await db_session.flush()
    db_session.add(App(team_id=team.id, app_id="reg-gone", app_name="gone",
                       api_key_hash="x", api_key_prefix="mds_gone_000000",
                       is_active=False, deleted_at=datetime.now(timezone.utc)))
    await db_session.commit()

    r = await _register(client, "reg-gone", team_slug="reg-deleted-team")
    assert r.status_code == 409, r.text
    assert "reserved" in r.json()["detail"]
