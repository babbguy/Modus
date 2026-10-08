"""Tests for orchestrator.api.policies — all endpoint handlers."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    App,
    GovernancePolicy,
    PolicyDecision,
    RealTimeSpend,
    Team,
)


def _team(**kw) -> Team:
    defaults = {"id": str(uuid.uuid4()), "slug": "pol-team", "name": "Policy Team"}
    defaults.update(kw)
    return Team(**defaults)


def _app(team_id: str, **kw) -> App:
    defaults = {
        "id": str(uuid.uuid4()),
        "team_id": team_id,
        "app_id": "pol-app",
        "app_name": "Policy App",
        "environment": "production",
        "api_key_hash": "x",
        "api_key_prefix": "mds_test",
    }
    defaults.update(kw)
    return App(**defaults)


def _policy(team_id: str = None, **kw) -> GovernancePolicy:
    defaults = {
        "id": str(uuid.uuid4()),
        "name": "Budget Cap $100",
        "scope": "platform",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "config": {"cap_usd": "100.00", "period": "daily"},
        "is_active": True,
        "created_by": "test",
    }
    if team_id:
        defaults["team_id"] = team_id
        defaults["scope"] = "team"
    defaults.update(kw)
    return GovernancePolicy(**defaults)


@pytest_asyncio.fixture
async def seed_policy(db_session: AsyncSession):
    team = _team()
    db_session.add(team)
    await db_session.flush()
    app = _app(str(team.id))
    db_session.add(app)
    pol = _policy(team_id=str(team.id))
    db_session.add(pol)
    await db_session.commit()
    return {"team": team, "app": app, "policy": pol}


# ── GET /policies ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_policies(client, seed_policy):
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["policy_type"] == "budget_cap"


@pytest.mark.asyncio
async def test_list_policies_by_scope(client, seed_policy):
    resp = await client.get("/api/v1/policies?scope=team")
    assert resp.status_code == 200
    data = resp.json()
    assert all(p["scope"] == "team" for p in data)


@pytest.mark.asyncio
async def test_list_policies_include_inactive(client, seed_policy):
    resp = await client.get("/api/v1/policies?active_only=false")
    assert resp.status_code == 200


# ── POST /policies ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_policy_budget_cap(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "Monthly Cap",
        "scope": "team",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 50,
        "team_id": team_id,
        "config": {"cap_usd": "500.00", "period": "monthly"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Monthly Cap"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_create_policy_rate_limit(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "Rate limit",
        "scope": "team",
        "policy_type": "rate_limit",
        "effect": "throttle",
        "priority": 80,
        "team_id": team_id,
        "config": {"max_calls": 1000, "window_seconds": 3600},
    })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_policy_model_denylist(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "Block expensive models",
        "scope": "team",
        "policy_type": "model_denylist",
        "effect": "deny",
        "priority": 60,
        "team_id": team_id,
        "config": {"models": ["gpt-4", "claude-opus-4-6"]},
    })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_policy_degradation_ladder(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "Degradation ladder",
        "scope": "team",
        "policy_type": "degradation_ladder",
        "effect": "deny",
        "priority": 70,
        "team_id": team_id,
        "config": {
            "budget_usd": "500.00",
            "period": "monthly",
            "tiers": [
                {"pct": 70, "model": "gpt-4o-mini"},
                {"pct": 90, "model": "gpt-3.5-turbo"},
                {"pct": 100, "action": "deny"},
            ],
        },
    })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_policy_platform_scope(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "Global deny",
        "scope": "platform",
        "policy_type": "provider_block",
        "effect": "deny",
        "priority": 10,
        "config": {"providers": ["cohere"]},
    })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_policy_missing_config_key(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "Bad config",
        "scope": "team",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "team_id": team_id,
        "config": {"cap_usd": "100"},  # missing 'period'
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_policy_bad_period(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "Bad period",
        "scope": "team",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "team_id": team_id,
        "config": {"cap_usd": "100", "period": "yearly"},
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_policy_team_scope_no_team_id(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "No team",
        "scope": "team",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "config": {"cap_usd": "100", "period": "daily"},
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_policy_app_scope_no_app_id(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "App scope no app",
        "scope": "app",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "team_id": team_id,
        "config": {"cap_usd": "100", "period": "daily"},
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_policy_invalid_type(client, seed_policy):
    team_id = str(seed_policy["team"].id)
    resp = await client.post("/api/v1/policies", json={
        "name": "Bad type",
        "scope": "team",
        "policy_type": "not_a_real_type",
        "effect": "deny",
        "priority": 100,
        "team_id": team_id,
        "config": {},
    })
    assert resp.status_code == 422


# ── PUT /policies/{id} ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_policy(client, seed_policy):
    pid = str(seed_policy["policy"].id)
    resp = await client.put(f"/api/v1/policies/{pid}", json={
        "name": "Renamed Cap",
        "priority": 200,
        "is_active": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Renamed Cap"
    assert data["priority"] == 200
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_update_policy_not_found(client):
    resp = await client.put(f"/api/v1/policies/{uuid.uuid4()}", json={
        "name": "Ghost",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_policy_config(client, seed_policy):
    pid = str(seed_policy["policy"].id)
    resp = await client.put(f"/api/v1/policies/{pid}", json={
        "config": {"cap_usd": "200.00", "period": "monthly"},
    })
    assert resp.status_code == 200
    assert resp.json()["config"]["cap_usd"] == "200.00"


# ── DELETE /policies/{id} ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deactivate_policy(client, seed_policy):
    pid = str(seed_policy["policy"].id)
    resp = await client.delete(f"/api/v1/policies/{pid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_deactivate_policy_not_found(client):
    resp = await client.delete(f"/api/v1/policies/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── GET /policy/decisions ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_decisions_empty(client):
    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_decisions_with_data(client, db_session, seed_policy):
    pd = PolicyDecision(
        id=str(uuid.uuid4()),
        policy_id=str(seed_policy["policy"].id),
        app_id=str(seed_policy["app"].id),
        team_id=str(seed_policy["team"].id),
        decision="deny",
        reason="Budget exceeded",
        request_provider="openai",
        request_model="gpt-4o",
    )
    db_session.add(pd)
    await db_session.commit()

    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_list_decisions_filter_by_decision(client, db_session, seed_policy):
    for d in ("allow", "deny"):
        db_session.add(PolicyDecision(
            id=str(uuid.uuid4()),
            app_id=str(seed_policy["app"].id),
            team_id=str(seed_policy["team"].id),
            decision=d,
            reason=f"Test {d}",
        ))
    await db_session.commit()

    resp = await client.get("/api/v1/policy/decisions?decision=deny")
    assert resp.status_code == 200
    assert all(r["decision"] == "deny" for r in resp.json())


# ── GET /policy/spend/{app_id} ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_time_spend_empty(client, seed_policy):
    app_id = str(seed_policy["app"].id)
    resp = await client.get(f"/api/v1/policy/spend/{app_id}")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_real_time_spend_with_data(client, db_session, seed_policy):
    now = datetime.now(timezone.utc)
    rts = RealTimeSpend(
        id=str(uuid.uuid4()),
        app_id=str(seed_policy["app"].id),
        team_id=str(seed_policy["team"].id),
        period="daily",
        window_key="2026-04-09",
        window_start=now.replace(hour=0, minute=0, second=0),
        window_end=now,
        total_cost=Decimal("25.50"),
        call_count=150,
        input_tokens=10000,
        output_tokens=5000,
    )
    db_session.add(rts)
    await db_session.commit()

    resp = await client.get(f"/api/v1/policy/spend/{str(seed_policy['app'].id)}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_real_time_spend_app_not_found(client):
    resp = await client.get(f"/api/v1/policy/spend/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── PUT /apps/{app_id}/enforcement ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_enforcement_state(client, seed_policy):
    app_id = str(seed_policy["app"].id)
    resp = await client.put(f"/api/v1/apps/{app_id}/enforcement", json={
        "enforcement_state": "admin_suspended",
        "reason": "Investigation in progress",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["enforcement_state"] == "admin_suspended"


@pytest.mark.asyncio
async def test_set_enforcement_state_back_to_active(client, seed_policy):
    app_id = str(seed_policy["app"].id)
    resp = await client.put(f"/api/v1/apps/{app_id}/enforcement", json={
        "enforcement_state": "active",
    })
    assert resp.status_code == 200
    assert resp.json()["enforcement_state"] == "active"


@pytest.mark.asyncio
async def test_set_enforcement_state_not_found(client):
    resp = await client.put(f"/api/v1/apps/{uuid.uuid4()}/enforcement", json={
        "enforcement_state": "active",
    })
    assert resp.status_code == 404


# ── GET /policy/ladder-status ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ladder_status_empty(client):
    resp = await client.get("/api/v1/policy/ladder-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "ladder_statuses" in data


@pytest.mark.asyncio
async def test_ladder_status_with_policy(client, db_session, seed_policy):
    team_id = str(seed_policy["team"].id)
    db_session.add(GovernancePolicy(
        id=str(uuid.uuid4()),
        name="Ladder pol",
        scope="team",
        policy_type="degradation_ladder",
        effect="deny",
        priority=50,
        team_id=team_id,
        config={
            "budget_usd": "100.00",
            "period": "monthly",
            "tiers": [
                {"pct": 80, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        },
        is_active=True,
        created_by="test",
    ))
    await db_session.commit()

    resp = await client.get(f"/api/v1/policy/ladder-status?team_id={team_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["ladder_statuses"]) >= 1
