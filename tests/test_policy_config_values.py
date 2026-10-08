"""
Tests -- policy config VALUES are validated at the API boundary (not only key presence),
so a malformed config can never reach the evaluator on the hot path.
"""
from __future__ import annotations

import pytest

from orchestrator.api.policies import _validate_policy_config
from fastapi import HTTPException


@pytest.mark.parametrize(
    "ptype,config",
    [
        ("budget_cap", {"cap_usd": "100.00", "period": "daily"}),
        ("budget_cap", {"cap_usd": 100, "period": "monthly"}),
        ("rate_limit", {"max_calls": 10, "window_seconds": 60}),
        ("token_cap", {"max_tokens": 1000, "period": "hourly"}),
        ("latency_cap", {"max_ms": 2000}),
        ("model_allowlist", {"models": ["gpt-4o"]}),
        ("provider_block", {"providers": ["openai"]}),
        ("environment_block", {"environments": ["dev"]}),
        ("amplification_gate", {"max_amplification": 3.5}),
        ("retry_circuit_breaker", {"max_retries": 3, "window_minutes": 5}),
        ("degradation_ladder", {"budget_usd": "500.00", "period": "monthly",
                                "tiers": [{"pct": 70, "model": "m"}, {"pct": 100, "action": "deny"}]}),
    ],
)
def test_valid_configs_pass(ptype, config):
    _validate_policy_config(ptype, config)


@pytest.mark.parametrize(
    "ptype,config,fragment",
    [
        ("budget_cap", {"cap_usd": "abc", "period": "daily"}, "cap_usd"),
        ("budget_cap", {"cap_usd": "-5", "period": "daily"}, "greater than 0"),
        ("budget_cap", {"cap_usd": "NaN", "period": "daily"}, "cap_usd"),
        ("budget_cap", {"cap_usd": True, "period": "daily"}, "cap_usd"),
        ("rate_limit", {"max_calls": 0, "window_seconds": 60}, "max_calls"),
        ("rate_limit", {"max_calls": "10", "window_seconds": 60}, "max_calls"),
        ("token_cap", {"max_tokens": 1.5, "period": "daily"}, "max_tokens"),
        ("model_denylist", {"models": []}, "models"),
        ("model_denylist", {"models": ["ok", " "]}, "models"),
        ("provider_block", {"providers": "openai"}, "providers"),
        ("amplification_gate", {"max_amplification": 0.5}, "max_amplification"),
        ("retry_circuit_breaker", {"max_retries": 3, "window_minutes": 0}, "window_minutes"),
        ("degradation_ladder", {"budget_usd": "0", "period": "daily", "tiers": [{"pct": 10, "model": "m"}]}, "budget_usd"),
        ("degradation_ladder", {"budget_usd": "5", "period": "daily", "tiers": [{"pct": 150, "model": "m"}]}, "pct"),
        ("degradation_ladder", {"budget_usd": "5", "period": "daily", "tiers": ["x"]}, "tiers[0]"),
    ],
)
def test_malformed_values_are_rejected_with_400(ptype, config, fragment):
    with pytest.raises(HTTPException) as exc:
        _validate_policy_config(ptype, config)
    assert exc.value.status_code == 400
    assert fragment in str(exc.value.detail)


async def test_create_policy_endpoint_returns_400_for_bad_value(client):
    resp = await client.post("/api/v1/policies", json={
        "name": "bad", "scope": "platform", "policy_type": "budget_cap",
        "config": {"cap_usd": "lots", "period": "daily"},
    })
    assert resp.status_code == 400
    assert "cap_usd" in resp.text
