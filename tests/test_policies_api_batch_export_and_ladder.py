"""
Policies API endpoints.

Targets orchestrator.api.policies: CRUD, decisions, real-time spend,
enforcement state, ladder status, batch apply, export, sync, and evaluate.
"""
from __future__ import annotations

import uuid

import pytest
# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _create_team(client, slug: str):
    resp = await client.post("/api/v1/teams", json={"name": slug, "slug": slug})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _insert_app(engine, team_id: str, name: str):
    """Insert an app directly in DB (registration needs master key)."""
    import uuid as _uuid
    import bcrypt
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from orchestrator.db.models import App

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        app_id = str(_uuid.uuid4())
        raw_key = "mds_test_" + "a" * 32
        key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()
        app = App(
            id=app_id,
            team_id=team_id,
            app_id="test-app-" + _uuid.uuid4().hex[:8],
            app_name=name,
            environment="production",
            api_key_hash=key_hash,
            api_key_prefix=raw_key[:12],
        )
        session.add(app)
        await session.commit()
        return {"id": app_id, "app_name": name}


async def _create_policy(client, team_id: str | None = None, **overrides):
    payload = {
        "name": overrides.get("name", "Test Budget Cap"),
        "scope": overrides.get("scope", "platform"),
        "policy_type": overrides.get("policy_type", "budget_cap"),
        "effect": overrides.get("effect", "deny"),
        "priority": overrides.get("priority", 100),
        "config": overrides.get("config", {
            "cap_usd": "500.00", "period": "monthly",
        }),
    }
    if team_id:
        payload["team_id"] = team_id
        payload["scope"] = "team"
    if "app_id" in overrides:
        payload["app_id"] = overrides["app_id"]
    resp = await client.post("/api/v1/policies", json=payload)
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# 1. POLICY CRUD
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_policies_empty(client):
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_create_policy_platform_budget_cap(client):
    resp = await _create_policy(client)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Budget Cap"
    assert data["scope"] == "platform"
    assert data["policy_type"] == "budget_cap"
    assert data["is_active"] is True


async def test_create_policy_rate_limit(client):
    resp = await _create_policy(client, policy_type="rate_limit", name="Rate Limit",
                                config={"max_calls": 100, "window_seconds": 3600})
    assert resp.status_code == 201
    assert resp.json()["policy_type"] == "rate_limit"


async def test_create_policy_model_allowlist(client):
    resp = await _create_policy(client, policy_type="model_allowlist",
                                name="Model Allowlist",
                                config={"models": ["gpt-4o-mini"]})
    assert resp.status_code == 201


async def test_create_policy_model_denylist(client):
    resp = await _create_policy(client, policy_type="model_denylist",
                                name="Model Denylist",
                                config={"models": ["gpt-4-turbo"]})
    assert resp.status_code == 201


async def test_create_policy_provider_block(client):
    resp = await _create_policy(client, policy_type="provider_block",
                                name="Block Cohere",
                                config={"providers": ["cohere"]})
    assert resp.status_code == 201


async def test_create_policy_token_cap(client):
    resp = await _create_policy(client, policy_type="token_cap",
                                name="Token Cap",
                                config={"max_tokens": 10000, "period": "daily"})
    assert resp.status_code == 201


async def test_create_policy_degradation_ladder(client):
    resp = await _create_policy(
        client, policy_type="degradation_ladder",
        name="Ladder",
        config={
            "budget_usd": "1000.00", "period": "monthly",
            "tiers": [
                {"pct": 80, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        },
    )
    assert resp.status_code == 201


async def test_create_policy_amplification_gate(client):
    resp = await _create_policy(client, policy_type="amplification_gate",
                                name="Amp Gate",
                                config={"max_amplification": 5.0})
    assert resp.status_code == 201


async def test_create_policy_team_scope(client):
    team = await _create_team(client, "policy-team-1")
    resp = await _create_policy(client, team_id=team["id"], name="Team Budget")
    assert resp.status_code == 201
    assert resp.json()["scope"] == "team"
    assert resp.json()["team_id"] == team["id"]


async def test_create_policy_team_scope_no_team_id(client):
    resp = await _create_policy(
        client, scope="team", name="Bad Team Policy",
        config={"cap_usd": "100", "period": "monthly"},
    )
    assert resp.status_code == 400


async def test_create_policy_app_scope_no_app_id(client):
    team = await _create_team(client, "policy-app-scope")
    resp = await client.post("/api/v1/policies", json={
        "name": "Bad App Policy",
        "scope": "app",
        "policy_type": "budget_cap",
        "config": {"cap_usd": "100", "period": "monthly"},
        "team_id": team["id"],
    })
    assert resp.status_code == 400


async def test_create_policy_missing_config_keys(client):
    resp = await _create_policy(client, policy_type="budget_cap",
                                name="Missing keys", config={})
    assert resp.status_code == 400


async def test_create_policy_invalid_period(client):
    resp = await _create_policy(client, policy_type="budget_cap",
                                name="Bad period",
                                config={"cap_usd": "100", "period": "yearly"})
    assert resp.status_code == 400


async def test_create_policy_bad_ladder_tiers(client):
    resp = await _create_policy(
        client, policy_type="degradation_ladder",
        name="Bad Ladder", config={
            "budget_usd": "1000", "period": "monthly", "tiers": [],
        },
    )
    assert resp.status_code == 400


async def test_create_policy_ladder_tier_missing_pct(client):
    resp = await _create_policy(
        client, policy_type="degradation_ladder",
        name="Bad Tier", config={
            "budget_usd": "1000", "period": "monthly",
            "tiers": [{"model": "gpt-4o"}],
        },
    )
    assert resp.status_code == 400


async def test_list_policies_with_filters(client):
    await _create_policy(client, name="Filter Test")
    resp = await client.get("/api/v1/policies?scope=platform&active_only=true")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


async def test_update_policy(client):
    create_resp = await _create_policy(client, name="Updatable")
    policy_id = create_resp.json()["id"]

    resp = await client.put(f"/api/v1/policies/{policy_id}", json={
        "name": "Updated Name",
        "priority": 50,
        "effect": "throttle",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Name"
    assert data["priority"] == 50
    assert data["effect"] == "throttle"


async def test_update_policy_not_found(client):
    resp = await client.put(f"/api/v1/policies/{uuid.uuid4()}", json={
        "name": "Ghost",
    })
    assert resp.status_code == 404


async def test_update_policy_config(client):
    create_resp = await _create_policy(client, name="Config Update")
    policy_id = create_resp.json()["id"]

    resp = await client.put(f"/api/v1/policies/{policy_id}", json={
        "config": {"cap_usd": "999.99", "period": "daily"},
    })
    assert resp.status_code == 200
    assert resp.json()["config"]["cap_usd"] == "999.99"


async def test_deactivate_policy(client):
    create_resp = await _create_policy(client, name="To Delete")
    policy_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 204

    # Verify it's deactivated (not visible in active_only)
    list_resp = await client.get("/api/v1/policies?active_only=true")
    ids = [p["id"] for p in list_resp.json()]
    assert policy_id not in ids

    # But visible with active_only=false
    list_resp2 = await client.get("/api/v1/policies?active_only=false")
    all_ids = [p["id"] for p in list_resp2.json()]
    assert policy_id in all_ids


async def test_deactivate_policy_not_found(client):
    resp = await client.delete(f"/api/v1/policies/{uuid.uuid4()}")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DECISIONS & SPEND
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_decisions_empty(client):
    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_decisions_with_filters(client):
    resp = await client.get(
        "/api/v1/policy/decisions?"
        f"app_id={uuid.uuid4()}&decision=deny&limit=10"
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_real_time_spend_app_not_found(client):
    resp = await client.get(f"/api/v1/policy/spend/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_real_time_spend_valid_app(client, engine):
    team = await _create_team(client, "spend-team")
    app = await _insert_app(engine, team["id"], "spend-app")
    resp = await client.get(f"/api/v1/policy/spend/{app['id']}")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ENFORCEMENT STATE
# ═══════════════════════════════════════════════════════════════════════════════

async def test_set_enforcement_state(client, engine):
    team = await _create_team(client, "enforce-team")
    app = await _insert_app(engine, team["id"], "enforce-app")

    resp = await client.put(f"/api/v1/apps/{app['id']}/enforcement", json={
        "enforcement_state": "budget_suspended",
        "reason": "Budget exceeded",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["enforcement_state"] == "budget_suspended"
    assert data["reason"] == "Budget exceeded"


async def test_set_enforcement_state_active(client, engine):
    team = await _create_team(client, "enforce-active")
    app = await _insert_app(engine, team["id"], "enforce-app-active")

    await client.put(f"/api/v1/apps/{app['id']}/enforcement", json={
        "enforcement_state": "budget_suspended",
    })
    resp = await client.put(f"/api/v1/apps/{app['id']}/enforcement", json={
        "enforcement_state": "active",
    })
    assert resp.status_code == 200
    assert resp.json()["enforcement_state"] == "active"


async def test_set_enforcement_state_app_not_found(client):
    resp = await client.put(f"/api/v1/apps/{uuid.uuid4()}/enforcement", json={
        "enforcement_state": "budget_suspended",
    })
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 4. LADDER STATUS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_ladder_status_empty(client):
    resp = await client.get("/api/v1/policy/ladder-status")
    assert resp.status_code == 200
    data = resp.json()
    # Response is either a list or a dict with 'ladder_statuses' key
    if isinstance(data, dict):
        assert data.get("ladder_statuses") == []
    else:
        assert data == []


async def test_ladder_status_with_policy(client):
    team = await _create_team(client, "ladder-team")
    await _create_policy(
        client, team_id=team["id"], policy_type="degradation_ladder",
        name="Ladder Status Test", config={
            "budget_usd": "1000.00", "period": "monthly",
            "tiers": [
                {"pct": 80, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        },
    )
    resp = await client.get(
        f"/api/v1/policy/ladder-status?team_id={team['id']}"
    )
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 5. POLICY EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

async def test_export_policies(client):
    await _create_policy(client, name="Export Test")
    resp = await client.get("/api/v1/policies/export")
    assert resp.status_code == 200
    data = resp.json()
    assert "policies" in data
    assert isinstance(data["policies"], list)


async def test_export_policies_with_scope_filter(client):
    resp = await client.get("/api/v1/policies/export?scope=platform")
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 6. VALIDATE POLICY CONFIG (unit test)
# ═══════════════════════════════════════════════════════════════════════════════

def test_validate_policy_config_rate_limit_missing():
    from fastapi import HTTPException
    from orchestrator.api.policies import _validate_policy_config
    with pytest.raises(HTTPException) as exc_info:
        _validate_policy_config("rate_limit", {"max_calls": 100})
    assert exc_info.value.status_code == 400


def test_validate_policy_config_valid_budget_cap():
    from orchestrator.api.policies import _validate_policy_config
    _validate_policy_config("budget_cap", {"cap_usd": "100", "period": "monthly"})


def test_validate_policy_config_unknown_type():
    from orchestrator.api.policies import _validate_policy_config
    # Unknown types have no required keys, so should pass
    _validate_policy_config("unknown_type", {"anything": True})


def test_validate_policy_config_environment_block():
    from orchestrator.api.policies import _validate_policy_config
    _validate_policy_config("environment_block", {"environments": ["staging"]})


def test_validate_policy_config_latency_cap():
    from orchestrator.api.policies import _validate_policy_config
    _validate_policy_config("latency_cap", {"max_ms": 500})


def test_validate_policy_config_retry_circuit_breaker():
    from orchestrator.api.policies import _validate_policy_config
    _validate_policy_config("retry_circuit_breaker", {"max_retries": 3})
