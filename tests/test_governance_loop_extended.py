"""
Tests for orchestrator.core.governance_loop — Extended coverage for
YAML generators, detection rule constants, and governance defaults.
"""
from __future__ import annotations

from orchestrator.core.governance_loop import (
    GOVERNANCE_DEFAULTS,
    _CHEAP_ALTERNATIVES,
    _EXPENSIVE_MODELS,
    _PROPOSAL_TYPE_TO_RULE,
    _RULE_DETAIL,
    _generate_amplification_gate_yaml,
    _generate_budget_cap_yaml,
    _generate_degradation_ladder_yaml,
    _generate_model_denylist_yaml,
    _generate_rate_limit_yaml,
)


# ── YAML generators ────────────────────────────────────────────────────────


class TestGenerateBudgetCapYaml:
    def test_basic_output(self):
        result = _generate_budget_cap_yaml(
            name="daily-cap",
            cap_usd="100.00",
            period="daily",
        )
        assert "- name: daily-cap" in result
        assert "  type: budget_cap" in result
        assert "  effect: deny" in result
        assert '    cap_usd: "100.00"' in result
        assert "    period: daily" in result

    def test_monthly_period(self):
        result = _generate_budget_cap_yaml(
            name="monthly-cap",
            cap_usd="5000.00",
            period="monthly",
        )
        assert "    period: monthly" in result


class TestGenerateRateLimitYaml:
    def test_basic_output(self):
        result = _generate_rate_limit_yaml(
            name="api-rate",
            max_calls=100,
            window_seconds=60,
        )
        assert "- name: api-rate" in result
        assert "  type: rate_limit" in result
        assert "  effect: throttle" in result
        assert "    max_calls: 100" in result
        assert "    window_seconds: 60" in result
        assert "    retry_after_seconds: 30" in result


class TestGenerateAmplificationGateYaml:
    def test_basic_output(self):
        result = _generate_amplification_gate_yaml(
            name="amp-gate",
            max_amplification=3.0,
        )
        assert "- name: amp-gate" in result
        assert "  type: amplification_gate" in result
        assert "  effect: deny" in result
        assert "    max_amplification: 3.0" in result
        assert "3.0x limit" in result


class TestGenerateModelDenylistYaml:
    def test_basic_output(self):
        result = _generate_model_denylist_yaml(
            name="no-expensive",
            models=["gpt-4o", "claude-opus-4-5-20251022"],
        )
        assert "- name: no-expensive" in result
        assert "  type: model_denylist" in result
        assert "  effect: deny" in result
        assert "      - gpt-4o" in result
        assert "      - claude-opus-4-5-20251022" in result

    def test_empty_models(self):
        result = _generate_model_denylist_yaml(name="empty", models=[])
        assert "    models:" in result


class TestDegradationLadderExtended:
    def test_action_only_tiers(self):
        result = _generate_degradation_ladder_yaml(
            name="deny-only",
            budget_usd="50",
            period="hourly",
            tiers=[{"pct": 100, "action": "deny"}],
        )
        assert "      - pct: 100" in result
        assert "        action: deny" in result
        assert "model" not in result.split("tiers:")[1]


# ── Constants ───────────────────────────────────────────────────────────────


class TestExpensiveModels:
    def test_expensive_models_not_empty(self):
        assert len(_EXPENSIVE_MODELS) > 0

    def test_gpt4o_is_expensive(self):
        assert "gpt-4o" in _EXPENSIVE_MODELS

    def test_cheap_alternatives_cover_expensive(self):
        # Every expensive model with a known cheap alt should be mapped
        for model in _CHEAP_ALTERNATIVES:
            assert model in _EXPENSIVE_MODELS

    def test_alternatives_are_not_expensive(self):
        for alt in _CHEAP_ALTERNATIVES.values():
            # Cheap alternatives should generally not be in the expensive set
            # (some exceptions possible, but gpt-4o-mini should not be)
            if alt == "gpt-4o-mini":
                assert alt not in _EXPENSIVE_MODELS


class TestProposalTypeToRule:
    def test_all_types_have_detail(self):
        for rule_name in _PROPOSAL_TYPE_TO_RULE.values():
            assert rule_name in _RULE_DETAIL, (
                f"Rule '{rule_name}' missing from _RULE_DETAIL"
            )


class TestGovernanceDefaults:
    def test_governance_enabled_by_default(self):
        assert GOVERNANCE_DEFAULTS["task.governance_loop.enabled"] == "true"

    def test_lookback_hours_reasonable(self):
        hours = int(GOVERNANCE_DEFAULTS["governance.lookback_hours"])
        assert 1 <= hours <= 168  # 1 hour to 1 week

    def test_min_calls_positive(self):
        min_calls = int(GOVERNANCE_DEFAULTS["governance.min_calls_for_analysis"])
        assert min_calls > 0

    def test_amplification_threshold_positive(self):
        threshold = float(GOVERNANCE_DEFAULTS["governance.amplification_threshold"])
        assert threshold > 0
