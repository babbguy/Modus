"""
Policies API deeper coverage.

Targets orchestrator.api.policies: CRUD, evaluation, decisions, spend,
config validation, sync.
"""
from __future__ import annotations

import uuid

import pytest
# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _create_team(client, slug: str, name: str):
    resp = await client.post("/api/v1/teams", json={"name": name, "slug": slug})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _create_policy(client, team_id: str, name: str = "Test Policy",
                          policy_type: str = "budget_cap",
                          config: dict | None = None):
    payload = {
        "name": name,
        "scope": "team",
        "policy_type": policy_type,
        "effect": "deny",
        "priority": 100,
        "team_id": team_id,
        "config": config or {"cap_usd": "1000.00", "period": "monthly"},
    }
    resp = await client.post("/api/v1/policies", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. POLICY CRUD
# ═══════════════════════════════════════════════════════════════════════════════

async def test_create_policy_budget_cap(client):
    team = await _create_team(client, "pol-t1", "Policy Team 1")
    p = await _create_policy(client, team["id"])
    assert p["name"] == "Test Policy"
    assert p["policy_type"] == "budget_cap"
    assert p["is_active"] is True
    assert p["scope"] == "team"


async def test_create_policy_rate_limit(client):
    team = await _create_team(client, "pol-t2", "Policy Team 2")
    p = await _create_policy(
        client, team["id"], "Rate Limit",
        policy_type="rate_limit",
        config={"max_calls": 100, "window_seconds": 60},
    )
    assert p["policy_type"] == "rate_limit"


async def test_create_policy_model_allowlist(client):
    team = await _create_team(client, "pol-t3", "Policy Team 3")
    p = await _create_policy(
        client, team["id"], "Model Allow",
        policy_type="model_allowlist",
        config={"models": ["gpt-4o-mini"]},
    )
    assert p["policy_type"] == "model_allowlist"


async def test_create_policy_model_denylist(client):
    team = await _create_team(client, "pol-t4", "Policy Team 4")
    p = await _create_policy(
        client, team["id"], "Model Deny",
        policy_type="model_denylist",
        config={"models": ["gpt-4"]},
    )
    assert p["policy_type"] == "model_denylist"


async def test_create_policy_degradation_ladder(client):
    team = await _create_team(client, "pol-t5", "Policy Team 5")
    p = await _create_policy(
        client, team["id"], "Degradation",
        policy_type="degradation_ladder",
        config={
            "budget_usd": "500.00",
            "period": "daily",
            "tiers": [
                {"pct": 50, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        },
    )
    assert p["policy_type"] == "degradation_ladder"


async def test_create_policy_missing_config_keys(client):
    team = await _create_team(client, "pol-t6", "Policy Team 6")
    resp = await client.post("/api/v1/policies", json={
        "name": "Bad Policy",
        "scope": "team",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "team_id": team["id"],
        "config": {},  # missing cap_usd and period
    })
    assert resp.status_code == 400


async def test_create_platform_policy(client):
    """Platform-scope policy (admin can create)."""
    p_resp = await client.post("/api/v1/policies", json={
        "name": "Platform Rate Limit",
        "scope": "platform",
        "policy_type": "rate_limit",
        "effect": "throttle",
        "priority": 50,
        "config": {"max_calls": 1000, "window_seconds": 3600},
    })
    assert p_resp.status_code == 201


async def test_create_app_scope_requires_app_id(client):
    team = await _create_team(client, "pol-app-t", "Policy App Team")
    resp = await client.post("/api/v1/policies", json={
        "name": "App Policy",
        "scope": "app",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "team_id": team["id"],
        "config": {"cap_usd": "100", "period": "daily"},
        # Missing app_id
    })
    assert resp.status_code == 400


async def test_create_team_scope_requires_team_id(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "Team Policy No ID",
        "scope": "team",
        "policy_type": "budget_cap",
        "effect": "deny",
        "priority": 100,
        "config": {"cap_usd": "100", "period": "daily"},
        # Missing team_id
    })
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# 2. LIST POLICIES
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_policies_empty(client):
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_list_policies_with_scope_filter(client):
    team = await _create_team(client, "pol-filt", "Policy Filter")
    await _create_policy(client, team["id"])
    resp = await client.get("/api/v1/policies?scope=team")
    assert resp.status_code == 200
    data = resp.json()
    assert all(p["scope"] == "team" for p in data)


async def test_list_policies_with_team_filter(client):
    team = await _create_team(client, "pol-tf", "Policy TF")
    await _create_policy(client, team["id"], name="Team Filtered")
    resp = await client.get(f"/api/v1/policies?team_id={team['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert all(p["team_id"] == team["id"] for p in data)


async def test_list_policies_include_inactive(client):
    team = await _create_team(client, "pol-inact", "Policy Inactive")
    p = await _create_policy(client, team["id"], name="Will Deactivate")
    await client.delete(f"/api/v1/policies/{p['id']}")
    # Active only (default)
    resp1 = await client.get("/api/v1/policies")
    active_ids = [x["id"] for x in resp1.json()]
    # Include inactive
    resp2 = await client.get("/api/v1/policies?active_only=false")
    all_ids = [x["id"] for x in resp2.json()]
    assert p["id"] not in active_ids
    assert p["id"] in all_ids


# ═══════════════════════════════════════════════════════════════════════════════
# 3. UPDATE POLICY
# ═══════════════════════════════════════════════════════════════════════════════

async def test_update_policy_name(client):
    team = await _create_team(client, "pol-upd", "Policy Update")
    p = await _create_policy(client, team["id"])
    resp = await client.put(f"/api/v1/policies/{p['id']}", json={
        "name": "Updated Policy Name",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Policy Name"


async def test_update_policy_config(client):
    team = await _create_team(client, "pol-cfg", "Policy Config")
    p = await _create_policy(client, team["id"])
    resp = await client.put(f"/api/v1/policies/{p['id']}", json={
        "config": {"cap_usd": "2000.00", "period": "monthly"},
    })
    assert resp.status_code == 200
    assert resp.json()["config"]["cap_usd"] == "2000.00"


async def test_update_policy_deactivate(client):
    team = await _create_team(client, "pol-deact2", "Policy Deact2")
    p = await _create_policy(client, team["id"])
    resp = await client.put(f"/api/v1/policies/{p['id']}", json={
        "is_active": False,
    })
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_update_policy_not_found(client):
    resp = await client.put(f"/api/v1/policies/{uuid.uuid4()}", json={
        "name": "Nope",
    })
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DELETE (DEACTIVATE) POLICY
# ═══════════════════════════════════════════════════════════════════════════════

async def test_deactivate_policy(client):
    team = await _create_team(client, "pol-del", "Policy Delete")
    p = await _create_policy(client, team["id"])
    resp = await client.delete(f"/api/v1/policies/{p['id']}")
    assert resp.status_code == 204


async def test_deactivate_policy_not_found(client):
    resp = await client.delete(f"/api/v1/policies/{uuid.uuid4()}")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 5. POLICY DECISIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_decisions_empty(client):
    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_list_decisions_with_filters(client):
    team = await _create_team(client, "pol-dec", "Policy Dec")
    resp = await client.get(f"/api/v1/policy/decisions?team_id={team['id']}&decision=deny&limit=10")
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CONFIG VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_validate_policy_config_valid():
    from orchestrator.api.policies import _validate_policy_config
    # Should not raise
    _validate_policy_config("budget_cap", {"cap_usd": "100", "period": "daily"})
    _validate_policy_config("rate_limit", {"max_calls": 10, "window_seconds": 60})
    _validate_policy_config("model_allowlist", {"models": ["gpt-4o"]})
    _validate_policy_config("model_denylist", {"models": ["gpt-4"]})
    _validate_policy_config("provider_block", {"providers": ["openai"]})
    _validate_policy_config("environment_block", {"environments": ["production"]})
    _validate_policy_config("token_cap", {"max_tokens": 1000, "period": "daily"})
    _validate_policy_config("latency_cap", {"max_ms": 500})
    _validate_policy_config("amplification_gate", {"max_amplification": 3.0})
    _validate_policy_config("retry_circuit_breaker", {"max_retries": 5})
    _validate_policy_config("degradation_ladder", {
        "budget_usd": "100", "period": "daily",
        "tiers": [{"pct": 100, "action": "deny"}],
    })


def test_validate_policy_config_missing_keys():
    from orchestrator.api.policies import _validate_policy_config
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _validate_policy_config("budget_cap", {"cap_usd": "100"})  # missing period
    assert exc_info.value.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# 7. SCHEMA VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_evaluate_request_body_lowercase():
    from orchestrator.api.policies import EvaluateRequestBody
    body = EvaluateRequestBody(provider="  OpenAI  ")
    assert body.provider == "openai"


def test_policy_create_invalid_type():
    from orchestrator.api.policies import PolicyCreate
    with pytest.raises(Exception):
        PolicyCreate(
            name="Bad", scope="platform", policy_type="not_real_type",
            effect="deny", priority=100, config={},
        )
