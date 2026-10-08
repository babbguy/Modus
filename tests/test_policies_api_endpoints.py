"""
Policies API (orchestrator/api/policies.py)

Integration tests for governance policy CRUD, evaluate, decisions, and
enforcement state endpoints via test client.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from orchestrator.db.models import (
    App,
    GovernancePolicy,
    PolicyDecision,
    RealTimeSpend,
    Team,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

TEAM_ID = str(uuid.uuid4())
APP_UUID = str(uuid.uuid4())


@pytest_asyncio.fixture
async def seeded_policies(db_session):
    """Seed team, app, and a sample policy for tests."""
    team = Team(id=TEAM_ID, slug="pol-team", name="Policy Team")
    db_session.add(team)

    app = App(
        id=APP_UUID,
        team_id=TEAM_ID,
        app_id="pol-app",
        app_name="Policy App",
        environment="production",
        api_key_hash="fake_hash",
        api_key_prefix="mds_fake",
    )
    db_session.add(app)

    policy = GovernancePolicy(
        name="Test Budget Cap",
        scope="platform",
        policy_type="budget_cap",
        effect="deny",
        priority=100,
        config={"cap_usd": "1000.00", "period": "monthly"},
        is_active=True,
        created_by="test",
    )
    db_session.add(policy)
    await db_session.flush()
    return {"team": team, "app": app, "policy": policy}


# ── List policies ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_policies_empty(client):
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_policies_with_data(client, seeded_policies):
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["name"] == "Test Budget Cap"


@pytest.mark.asyncio
async def test_list_policies_filter_scope(client, seeded_policies):
    resp = await client.get("/api/v1/policies?scope=platform")
    assert resp.status_code == 200
    data = resp.json()
    assert all(p["scope"] == "platform" for p in data)


@pytest.mark.asyncio
async def test_list_policies_include_inactive(client, seeded_policies):
    resp = await client.get("/api/v1/policies?active_only=false")
    assert resp.status_code == 200


# ── Create policy ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_policy_budget_cap(client, seeded_policies):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "Team Budget Cap",
            "scope": "team",
            "policy_type": "budget_cap",
            "effect": "deny",
            "priority": 50,
            "team_id": TEAM_ID,
            "config": {"cap_usd": "500.00", "period": "monthly"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Team Budget Cap"
    assert data["scope"] == "team"
    assert data["policy_type"] == "budget_cap"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_create_policy_rate_limit(client, seeded_policies):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "Rate Limit Policy",
            "scope": "team",
            "policy_type": "rate_limit",
            "effect": "throttle",
            "priority": 80,
            "team_id": TEAM_ID,
            "config": {"max_calls": 100, "window_seconds": 60},
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_policy_model_allowlist(client, seeded_policies):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "Allowed Models",
            "scope": "team",
            "policy_type": "model_allowlist",
            "effect": "deny",
            "priority": 10,
            "team_id": TEAM_ID,
            "config": {"models": ["gpt-4o", "gpt-4o-mini"]},
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_policy_model_denylist(client, seeded_policies):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "Blocked Models",
            "scope": "platform",
            "policy_type": "model_denylist",
            "effect": "deny",
            "priority": 5,
            "config": {"models": ["gpt-4-turbo"]},
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_policy_missing_config_key(client, seeded_policies):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "Bad Config",
            "scope": "platform",
            "policy_type": "budget_cap",
            "effect": "deny",
            "config": {},  # missing cap_usd and period
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_policy_invalid_period(client, seeded_policies):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "Bad Period",
            "scope": "platform",
            "policy_type": "budget_cap",
            "effect": "deny",
            "config": {"cap_usd": "100", "period": "weekly"},
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_policy_team_scope_no_team_id(client):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "No Team ID",
            "scope": "team",
            "policy_type": "rate_limit",
            "effect": "deny",
            "config": {"max_calls": 100, "window_seconds": 60},
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_policy_app_scope_no_app_id(client, seeded_policies):
    resp = await client.post(
        "/api/v1/policies",
        json={
            "name": "No App ID",
            "scope": "app",
            "policy_type": "rate_limit",
            "effect": "deny",
            "team_id": TEAM_ID,
            "config": {"max_calls": 100, "window_seconds": 60},
        },
    )
    assert resp.status_code == 400


# ── Update policy ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_policy(client, seeded_policies):
    pid = str(seeded_policies["policy"].id)
    resp = await client.put(
        f"/api/v1/policies/{pid}",
        json={
            "name": "Updated Budget Cap",
            "effect": "warn",
            "priority": 200,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Budget Cap"
    assert data["effect"] == "warn"
    assert data["priority"] == 200


@pytest.mark.asyncio
async def test_update_policy_not_found(client):
    resp = await client.put(
        f"/api/v1/policies/{uuid.uuid4()}",
        json={"name": "Ghost"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_policy_config(client, seeded_policies):
    pid = str(seeded_policies["policy"].id)
    resp = await client.put(
        f"/api/v1/policies/{pid}",
        json={"config": {"cap_usd": "2000.00", "period": "monthly"}},
    )
    assert resp.status_code == 200
    assert resp.json()["config"]["cap_usd"] == "2000.00"


@pytest.mark.asyncio
async def test_update_policy_deactivate(client, seeded_policies):
    pid = str(seeded_policies["policy"].id)
    resp = await client.put(
        f"/api/v1/policies/{pid}",
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


# ── Delete (deactivate) policy ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deactivate_policy(client, seeded_policies):
    pid = str(seeded_policies["policy"].id)
    resp = await client.delete(f"/api/v1/policies/{pid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_deactivate_policy_not_found(client):
    resp = await client.delete(f"/api/v1/policies/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Policy decisions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_decisions_empty(client):
    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_decisions_with_data(client, seeded_policies, db_session):
    pd = PolicyDecision(
        policy_id=str(seeded_policies["policy"].id),
        app_id=APP_UUID,
        team_id=TEAM_ID,
        decision="deny",
        reason="Budget exceeded",
        request_provider="openai",
        request_model="gpt-4o",
        decided_at=datetime.now(timezone.utc),
    )
    db_session.add(pd)
    await db_session.flush()

    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_list_decisions_filter_app(client, seeded_policies, db_session):
    pd = PolicyDecision(
        app_id=APP_UUID, team_id=TEAM_ID,
        decision="allow", reason="ok",
        decided_at=datetime.now(timezone.utc),
    )
    db_session.add(pd)
    await db_session.flush()

    resp = await client.get(f"/api/v1/policy/decisions?app_id={APP_UUID}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_decisions_filter_decision(client, seeded_policies, db_session):
    pd = PolicyDecision(
        app_id=APP_UUID, team_id=TEAM_ID,
        decision="throttle", reason="rate limit",
        decided_at=datetime.now(timezone.utc),
    )
    db_session.add(pd)
    await db_session.flush()

    resp = await client.get("/api/v1/policy/decisions?decision=throttle")
    assert resp.status_code == 200


# ── Real-time spend ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_time_spend(client, seeded_policies, db_session):
    now = datetime.now(timezone.utc)
    rts = RealTimeSpend(
        app_id=APP_UUID,
        team_id=TEAM_ID,
        period="daily",
        window_key=f"daily:{now.strftime('%Y-%m-%d')}",
        window_start=now.replace(hour=0, minute=0, second=0),
        window_end=now.replace(hour=23, minute=59, second=59),
        total_cost=Decimal("12.50"),
        call_count=100,
        input_tokens=50000,
        output_tokens=20000,
    )
    db_session.add(rts)
    await db_session.flush()

    resp = await client.get(f"/api/v1/policy/spend/{APP_UUID}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_real_time_spend_app_not_found(client):
    resp = await client.get(f"/api/v1/policy/spend/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── Enforcement state ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_enforcement_state(client, seeded_policies):
    resp = await client.put(
        f"/api/v1/apps/{APP_UUID}/enforcement",
        json={
            "enforcement_state": "admin_suspended",
            "reason": "Testing suspension",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["enforcement_state"] == "admin_suspended"


@pytest.mark.asyncio
async def test_set_enforcement_state_reactivate(client, seeded_policies):
    resp = await client.put(
        f"/api/v1/apps/{APP_UUID}/enforcement",
        json={"enforcement_state": "active"},
    )
    assert resp.status_code == 200
    assert resp.json()["enforcement_state"] == "active"


@pytest.mark.asyncio
async def test_set_enforcement_state_app_not_found(client):
    resp = await client.put(
        f"/api/v1/apps/{uuid.uuid4()}/enforcement",
        json={"enforcement_state": "active"},
    )
    assert resp.status_code == 404


# ── Ladder status ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ladder_status_empty(client):
    resp = await client.get("/api/v1/policy/ladder-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "ladder_statuses" in data


@pytest.mark.asyncio
async def test_ladder_status_with_policy(client, seeded_policies, db_session):
    """Create a degradation_ladder policy and verify status response."""
    ladder = GovernancePolicy(
        name="Test Ladder",
        scope="team",
        policy_type="degradation_ladder",
        effect="deny",
        priority=50,
        team_id=TEAM_ID,
        config={
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [
                {"pct": 80, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        },
        is_active=True,
        created_by="test",
    )
    db_session.add(ladder)
    await db_session.flush()

    resp = await client.get(f"/api/v1/policy/ladder-status?team_id={TEAM_ID}")
    assert resp.status_code == 200
    data = resp.json()
    assert "ladder_statuses" in data


# ── Policy export ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_policies(client, seeded_policies):
    resp = await client.get("/api/v1/policies/export")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "policies" in data
    assert isinstance(data["policies"], list)


@pytest.mark.asyncio
async def test_export_policies_scope_filter(client, seeded_policies):
    resp = await client.get("/api/v1/policies/export?scope=platform")
    assert resp.status_code == 200


# ── Config validation helper ─────────────────────────────────────────────────


def test_validate_policy_config_degradation_ladder():
    from orchestrator.api.policies import _validate_policy_config

    # Valid
    _validate_policy_config("degradation_ladder", {
        "budget_usd": "1000",
        "period": "monthly",
        "tiers": [{"pct": 80, "model": "gpt-4o-mini"}, {"pct": 100, "action": "deny"}],
    })

    # Missing pct
    with pytest.raises(Exception):
        _validate_policy_config("degradation_ladder", {
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [{"model": "gpt-4o-mini"}],
        })

    # Empty tiers
    with pytest.raises(Exception):
        _validate_policy_config("degradation_ladder", {
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [],
        })

    # Missing model and no deny action
    with pytest.raises(Exception):
        _validate_policy_config("degradation_ladder", {
            "budget_usd": "1000",
            "period": "monthly",
            "tiers": [{"pct": 80}],
        })
