"""
Tests for orchestrator.api.policies — Integration tests for policy CRUD
handlers, validation, and the _validate_policy_config helper.
"""
from __future__ import annotations

import pytest

from orchestrator.api.policies import (
    _validate_policy_config,
    PolicyCreate,
    PolicyUpdate,
)
# ── _validate_policy_config ──────────────────────────────────────────────────


class TestValidatePolicyConfig:
    def test_budget_cap_valid(self):
        _validate_policy_config("budget_cap", {"cap_usd": "100", "period": "daily"})

    def test_budget_cap_missing_period(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_policy_config("budget_cap", {"cap_usd": "100"})
        assert exc_info.value.status_code == 400
        assert "period" in str(exc_info.value.detail)

    def test_rate_limit_valid(self):
        _validate_policy_config("rate_limit", {"max_calls": 100, "window_seconds": 60})

    def test_rate_limit_missing_keys(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_policy_config("rate_limit", {"max_calls": 100})

    def test_model_allowlist_valid(self):
        _validate_policy_config("model_allowlist", {"models": ["gpt-4o"]})

    def test_model_denylist_valid(self):
        _validate_policy_config("model_denylist", {"models": ["gpt-4"]})

    def test_provider_block_valid(self):
        _validate_policy_config("provider_block", {"providers": ["openai"]})

    def test_environment_block_valid(self):
        _validate_policy_config("environment_block", {"environments": ["staging"]})

    def test_token_cap_valid(self):
        _validate_policy_config("token_cap", {"max_tokens": 10000, "period": "daily"})

    def test_latency_cap_valid(self):
        _validate_policy_config("latency_cap", {"max_ms": 5000})

    def test_degradation_ladder_valid(self):
        _validate_policy_config("degradation_ladder", {
            "budget_usd": "1000", "period": "monthly",
            "tiers": [{"pct": 80, "model": "gpt-4o-mini"}],
        })

    def test_degradation_ladder_missing_tiers(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_policy_config("degradation_ladder", {
                "budget_usd": "1000", "period": "monthly",
            })

    def test_amplification_gate_valid(self):
        _validate_policy_config("amplification_gate", {"max_amplification": 3.0})

    def test_retry_circuit_breaker_valid(self):
        _validate_policy_config("retry_circuit_breaker", {"max_retries": 3})

    def test_unknown_policy_type_passes(self):
        # Unknown types have no required keys, so should not raise
        _validate_policy_config("unknown_type", {})


# ── PolicyCreate schema ──────────────────────────────────────────────────────


class TestPolicyCreateSchema:
    def test_valid_budget_cap(self):
        p = PolicyCreate(
            name="Test Budget",
            scope="platform",
            policy_type="budget_cap",
            config={"cap_usd": "100", "period": "daily"},
        )
        assert p.name == "Test Budget"
        assert p.effect == "deny"  # default
        assert p.priority == 100  # default

    def test_invalid_policy_type(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PolicyCreate(
                name="Bad", scope="platform",
                policy_type="nonexistent",
                config={},
            )

    def test_team_scope_requires_team_id_not_in_schema(self):
        # Schema allows team_id=None, the handler validates it
        p = PolicyCreate(
            name="Team Budget", scope="team",
            policy_type="budget_cap",
            config={"cap_usd": "50", "period": "daily"},
        )
        assert p.team_id is None  # handler will reject this

    def test_priority_bounds(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PolicyCreate(
                name="Bad", scope="platform",
                policy_type="budget_cap", config={"cap_usd": "100", "period": "daily"},
                priority=0,
            )
        with pytest.raises(ValidationError):
            PolicyCreate(
                name="Bad", scope="platform",
                policy_type="budget_cap", config={"cap_usd": "100", "period": "daily"},
                priority=1000,
            )


class TestPolicyUpdateSchema:
    def test_all_none(self):
        p = PolicyUpdate()
        assert p.name is None
        assert p.config is None

    def test_partial_update(self):
        p = PolicyUpdate(name="Updated", priority=50)
        assert p.name == "Updated"
        assert p.priority == 50
        assert p.config is None

    def test_is_active_toggle(self):
        p = PolicyUpdate(is_active=False)
        assert p.is_active is False


# ── API integration tests ────────────────────────────────────────────────────


async def test_list_policies_empty(client):
    resp = await client.get("/api/v1/policies")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_policy_platform_scope(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "Global Budget Cap",
        "scope": "platform",
        "policy_type": "budget_cap",
        "effect": "deny",
        "config": {"cap_usd": "1000", "period": "monthly"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Global Budget Cap"
    assert data["scope"] == "platform"
    assert data["policy_type"] == "budget_cap"
    assert data["is_active"] is True
    return data["id"]


async def test_create_policy_missing_config_key(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "Bad Config",
        "scope": "platform",
        "policy_type": "budget_cap",
        "config": {"cap_usd": "100"},  # missing "period"
    })
    assert resp.status_code == 400


async def test_create_team_scope_without_team_id(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "Team Budget",
        "scope": "team",
        "policy_type": "budget_cap",
        "config": {"cap_usd": "100", "period": "daily"},
    })
    assert resp.status_code == 400


async def test_create_app_scope_without_app_id(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "App Budget",
        "scope": "app",
        "policy_type": "budget_cap",
        "config": {"cap_usd": "100", "period": "daily"},
        "team_id": "00000000-0000-0000-0000-000000000001",
    })
    assert resp.status_code == 400


async def test_update_policy(client):
    # Create first
    create_resp = await client.post("/api/v1/policies", json={
        "name": "Update Test",
        "scope": "platform",
        "policy_type": "rate_limit",
        "config": {"max_calls": 100, "window_seconds": 60},
    })
    assert create_resp.status_code == 201
    policy_id = create_resp.json()["id"]

    # Update
    update_resp = await client.put(f"/api/v1/policies/{policy_id}", json={
        "name": "Updated Name",
        "priority": 50,
    })
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["name"] == "Updated Name"
    assert data["priority"] == 50


async def test_update_nonexistent_policy(client):
    resp = await client.put("/api/v1/policies/00000000-0000-0000-0000-000000000999", json={
        "name": "Ghost",
    })
    assert resp.status_code == 404


async def test_deactivate_policy(client):
    create_resp = await client.post("/api/v1/policies", json={
        "name": "Delete Test",
        "scope": "platform",
        "policy_type": "model_denylist",
        "config": {"models": ["gpt-4"]},
    })
    policy_id = create_resp.json()["id"]

    del_resp = await client.delete(f"/api/v1/policies/{policy_id}")
    assert del_resp.status_code == 204

    # Verify it's no longer listed (active_only default)
    list_resp = await client.get("/api/v1/policies")
    ids = [p["id"] for p in list_resp.json()]
    assert policy_id not in ids


async def test_deactivate_nonexistent_policy(client):
    resp = await client.delete("/api/v1/policies/00000000-0000-0000-0000-000000000999")
    assert resp.status_code == 404


async def test_list_policies_with_scope_filter(client):
    await client.post("/api/v1/policies", json={
        "name": "Platform Filter Test",
        "scope": "platform",
        "policy_type": "provider_block",
        "config": {"providers": ["evil-provider"]},
    })
    resp = await client.get("/api/v1/policies", params={"scope": "platform"})
    assert resp.status_code == 200
    for p in resp.json():
        assert p["scope"] == "platform"


async def test_list_policies_inactive(client):
    resp = await client.get("/api/v1/policies", params={"active_only": False})
    assert resp.status_code == 200


async def test_policy_decisions_empty(client):
    resp = await client.get("/api/v1/policy/decisions")
    assert resp.status_code == 200
    assert resp.json() == []
