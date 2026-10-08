"""
Tests for sdk.modus.policy_schema — Policy file validation.
"""
from __future__ import annotations


from modus.policy_schema import (
    EFFECTS,
    PERIODS,
    POLICY_TYPES,
    SCOPES,
    _is_usd,
    _validate_config,
    validate_policy_file,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _minimal_policy(ptype: str = "budget_cap", **overrides) -> dict:
    """Build a minimal valid policy dict."""
    base = {
        "name": "Test Policy",
        "type": ptype,
        "scope": "team",
        "team": "engineering",
    }
    if ptype == "budget_cap":
        base["config"] = {"cap_usd": "100.00", "period": "daily"}
    elif ptype == "rate_limit":
        base["config"] = {"max_calls": 100, "window_seconds": 60}
    elif ptype == "token_cap":
        base["config"] = {"max_tokens": 10000, "period": "daily"}
    elif ptype == "latency_cap":
        base["config"] = {"max_ms": 500}
    elif ptype == "model_allowlist":
        base["config"] = {"models": ["gpt-4o"]}
    elif ptype == "model_denylist":
        base["config"] = {"models": ["gpt-4"]}
    elif ptype == "provider_block":
        base["config"] = {"providers": ["openai"]}
    elif ptype == "environment_block":
        base["config"] = {"environments": ["production"]}
    elif ptype == "degradation_ladder":
        base["config"] = {
            "budget_usd": "500.00",
            "period": "monthly",
            "tiers": [{"pct": 80, "model": "gpt-4o-mini"}],
        }
    elif ptype == "amplification_gate":
        base["config"] = {"max_amplification": 5.0}
    elif ptype == "retry_circuit_breaker":
        base["config"] = {"max_retries": 3}
    base.update(overrides)
    return base


def _valid_file(**overrides) -> dict:
    """Build a minimal valid policy file."""
    d = {
        "version": "1",
        "policies": [_minimal_policy()],
    }
    d.update(overrides)
    return d


# ── Schema constants ─────────────────────────────────────────────────────────


class TestSchemaConstants:
    def test_policy_types(self):
        assert "budget_cap" in POLICY_TYPES
        assert "rate_limit" in POLICY_TYPES
        assert len(POLICY_TYPES) == 11

    def test_effects(self):
        assert set(EFFECTS) == {"deny", "throttle", "warn"}

    def test_scopes(self):
        assert set(SCOPES) == {"platform", "team", "app"}

    def test_periods(self):
        assert set(PERIODS) == {"hourly", "daily", "monthly"}


# ── _is_usd ──────────────────────────────────────────────────────────────────


class TestIsUsd:
    def test_valid_amounts(self):
        assert _is_usd("100") is True
        assert _is_usd("100.00") is True
        assert _is_usd("0.50") is True
        assert _is_usd("999999.99") is True

    def test_invalid_amounts(self):
        assert _is_usd("") is False
        assert _is_usd("abc") is False
        assert _is_usd("100.001") is False
        assert _is_usd("-10") is False
        assert _is_usd("$100") is False


# ── validate_policy_file — valid ─────────────────────────────────────────────


class TestValidPolicyFile:
    def test_minimal_valid(self):
        errors = validate_policy_file(_valid_file())
        assert errors == []

    def test_all_policy_types_valid(self):
        for ptype in POLICY_TYPES:
            policy = _minimal_policy(ptype)
            data = {"version": "1", "policies": [policy]}
            errors = validate_policy_file(data)
            assert errors == [], f"Policy type {ptype} should be valid: {errors}"

    def test_with_defaults(self):
        data = {
            "version": "1",
            "defaults": {"scope": "team", "effect": "warn", "team": "eng"},
            "policies": [_minimal_policy()],
        }
        errors = validate_policy_file(data)
        assert errors == []

    def test_with_conditions(self):
        policy = _minimal_policy()
        policy["conditions"] = {"providers": ["openai"], "model_pattern": "gpt-4*"}
        data = {"version": "1", "policies": [policy]}
        errors = validate_policy_file(data)
        assert errors == []

    def test_with_action(self):
        policy = _minimal_policy()
        policy["action"] = {"message": "Budget exceeded", "retry_after_seconds": 60}
        data = {"version": "1", "policies": [policy]}
        errors = validate_policy_file(data)
        assert errors == []


# ── validate_policy_file — invalid ───────────────────────────────────────────


class TestInvalidPolicyFile:
    def test_not_a_dict(self):
        errors = validate_policy_file("not a dict")
        assert len(errors) == 1
        assert "mapping" in errors[0].lower()

    def test_missing_version(self):
        errors = validate_policy_file({"policies": [_minimal_policy()]})
        assert any("version" in e for e in errors)

    def test_wrong_version(self):
        errors = validate_policy_file({"version": "2", "policies": [_minimal_policy()]})
        assert any("version" in e for e in errors)

    def test_empty_policies(self):
        errors = validate_policy_file({"version": "1", "policies": []})
        assert any("non-empty" in e for e in errors)

    def test_missing_policies(self):
        errors = validate_policy_file({"version": "1"})
        assert any("policies" in e for e in errors)

    def test_unknown_top_level_key(self):
        data = _valid_file()
        data["unknown_key"] = "value"
        errors = validate_policy_file(data)
        assert any("unknown_key" in e.lower() for e in errors)

    def test_duplicate_policy_names(self):
        p1 = _minimal_policy(name="Same Name")
        p2 = _minimal_policy(name="Same Name")
        p2["type"] = "rate_limit"
        p2["config"] = {"max_calls": 10, "window_seconds": 60}
        data = {"version": "1", "policies": [p1, p2]}
        errors = validate_policy_file(data)
        assert any("duplicate" in e.lower() for e in errors)

    def test_invalid_type(self):
        data = {"version": "1", "policies": [{"name": "x", "type": "invalid_type", "team": "t"}]}
        errors = validate_policy_file(data)
        assert any("type" in e for e in errors)

    def test_invalid_scope(self):
        policy = _minimal_policy(scope="galaxy")
        data = {"version": "1", "policies": [policy]}
        errors = validate_policy_file(data)
        assert any("scope" in e for e in errors)

    def test_invalid_effect(self):
        policy = _minimal_policy(effect="explode")
        data = {"version": "1", "policies": [policy]}
        errors = validate_policy_file(data)
        assert any("effect" in e for e in errors)

    def test_app_scope_requires_app(self):
        policy = _minimal_policy(scope="app")
        data = {"version": "1", "policies": [policy]}
        errors = validate_policy_file(data)
        assert any("app" in e.lower() for e in errors)

    def test_unknown_policy_keys(self):
        policy = _minimal_policy()
        policy["unknown_field"] = "value"
        data = {"version": "1", "policies": [policy]}
        errors = validate_policy_file(data)
        assert any("unknown" in e.lower() for e in errors)


# ── _validate_config per type ────────────────────────────────────────────────


class TestValidateConfig:
    def test_budget_cap_missing_cap_usd(self):
        errors = _validate_config("p", "budget_cap", {"period": "daily"})
        assert any("cap_usd" in e for e in errors)

    def test_budget_cap_invalid_cap_usd(self):
        errors = _validate_config("p", "budget_cap", {"cap_usd": "abc", "period": "daily"})
        assert any("cap_usd" in e for e in errors)

    def test_rate_limit_missing_max_calls(self):
        errors = _validate_config("p", "rate_limit", {"window_seconds": 60})
        assert any("max_calls" in e for e in errors)

    def test_rate_limit_negative_window(self):
        errors = _validate_config("p", "rate_limit", {"max_calls": 10, "window_seconds": -1})
        assert any("window_seconds" in e for e in errors)

    def test_token_cap_missing_max_tokens(self):
        errors = _validate_config("p", "token_cap", {"period": "daily"})
        assert any("max_tokens" in e for e in errors)

    def test_latency_cap_missing_max_ms(self):
        errors = _validate_config("p", "latency_cap", {})
        assert any("max_ms" in e for e in errors)

    def test_model_allowlist_missing_models(self):
        errors = _validate_config("p", "model_allowlist", {})
        assert any("models" in e for e in errors)

    def test_model_allowlist_empty_models(self):
        errors = _validate_config("p", "model_allowlist", {"models": []})
        assert any("models" in e for e in errors)

    def test_provider_block_missing_providers(self):
        errors = _validate_config("p", "provider_block", {})
        assert any("providers" in e for e in errors)

    def test_environment_block_missing_environments(self):
        errors = _validate_config("p", "environment_block", {})
        assert any("environments" in e for e in errors)

    def test_degradation_ladder_missing_tiers(self):
        errors = _validate_config("p", "degradation_ladder", {
            "budget_usd": "500.00", "period": "monthly",
        })
        assert any("tiers" in e for e in errors)

    def test_degradation_ladder_invalid_tier(self):
        errors = _validate_config("p", "degradation_ladder", {
            "budget_usd": "500.00",
            "period": "monthly",
            "tiers": [{"pct": 150}],  # > 100
        })
        assert any("pct" in e for e in errors)

    def test_amplification_gate_missing(self):
        errors = _validate_config("p", "amplification_gate", {})
        assert any("max_amplification" in e for e in errors)

    def test_amplification_gate_below_one(self):
        errors = _validate_config("p", "amplification_gate", {"max_amplification": 0.5})
        assert any("max_amplification" in e for e in errors)

    def test_retry_circuit_breaker_missing(self):
        errors = _validate_config("p", "retry_circuit_breaker", {})
        assert any("max_retries" in e for e in errors)

    def test_removed_types_are_rejected(self):
        for ptype in ("trajectory_cap", "vigil_check", "webhook"):
            assert ptype not in POLICY_TYPES
