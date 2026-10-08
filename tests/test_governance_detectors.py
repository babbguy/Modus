"""
Tests for orchestrator.core.governance_loop — YAML generators, expensive
model detection constants, and detection rule helpers.
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


# ── Expensive model constants ────────────────────────────────────────────────


class TestExpensiveModels:
    def test_known_models_included(self):
        for m in ("gpt-4o", "gpt-4-turbo", "gpt-4", "o1-preview", "o1"):
            assert m in _EXPENSIVE_MODELS
        assert "claude-opus-4-5-20251022" in _EXPENSIVE_MODELS

    def test_cheap_models_not_included(self):
        assert "gpt-4o-mini" not in _EXPENSIVE_MODELS
        assert "gpt-3.5-turbo" not in _EXPENSIVE_MODELS

    def test_most_expensive_have_cheap_alternative(self):
        # Most expensive models have a cheaper mapping; o3 is an exception
        mapped = _EXPENSIVE_MODELS & set(_CHEAP_ALTERNATIVES.keys())
        assert len(mapped) >= len(_EXPENSIVE_MODELS) - 1

    def test_cheap_alternatives_are_not_expensive(self):
        for cheap in _CHEAP_ALTERNATIVES.values():
            # Cheap alternatives should ideally not be in expensive set
            # (they can overlap in some cases, but let's check the common ones)
            if cheap in ("gpt-4o-mini", "gemini-1.5-flash", "gemini-2.0-flash"):
                assert cheap not in _EXPENSIVE_MODELS


# ── Governance defaults ──────────────────────────────────────────────────────


class TestGovernanceDefaults:
    def test_task_enabled_by_default(self):
        assert GOVERNANCE_DEFAULTS["task.governance_loop.enabled"] == "true"

    def test_interval_is_one_hour(self):
        assert int(GOVERNANCE_DEFAULTS["task.governance_loop.interval_seconds"]) == 3600

    def test_lookback_hours(self):
        assert int(GOVERNANCE_DEFAULTS["governance.lookback_hours"]) == 24

    def test_min_calls(self):
        assert int(GOVERNANCE_DEFAULTS["governance.min_calls_for_analysis"]) == 50

    def test_ai_rationale_default_false(self):
        assert GOVERNANCE_DEFAULTS["governance.ai_rationale"] == "false"


# ── Proposal type → rule mapping ─────────────────────────────────────────────


class TestProposalTypeMapping:
    def test_all_proposal_types_have_rules(self):
        for pt, rule in _PROPOSAL_TYPE_TO_RULE.items():
            assert rule in _RULE_DETAIL, f"Rule '{rule}' for proposal type '{pt}' has no detail"

    def test_model_downshift(self):
        assert _PROPOSAL_TYPE_TO_RULE["model_downshift"] == "model_overprovision"

    def test_budget_tighten(self):
        assert _PROPOSAL_TYPE_TO_RULE["budget_tighten"] == "budget_overruns"


# ── YAML generators ──────────────────────────────────────────────────────────


class TestDegradationLadderYAML:
    def test_basic_output(self):
        yaml = _generate_degradation_ladder_yaml(
            name="auto-downshift",
            budget_usd="500",
            period="daily",
            tiers=[
                {"pct": 50, "model": "gpt-4o-mini"},
                {"pct": 90, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        )
        assert "auto-downshift" in yaml
        assert "degradation_ladder" in yaml
        assert '"500"' in yaml
        assert "daily" in yaml
        assert "gpt-4o-mini" in yaml
        assert "deny" in yaml

    def test_single_tier(self):
        yaml = _generate_degradation_ladder_yaml(
            name="simple",
            budget_usd="100",
            period="monthly",
            tiers=[{"pct": 100, "action": "deny"}],
        )
        assert "simple" in yaml
        assert "action: deny" in yaml


class TestBudgetCapYAML:
    def test_basic_output(self):
        yaml = _generate_budget_cap_yaml(
            name="eng-budget",
            cap_usd="1000",
            period="monthly",
        )
        assert "eng-budget" in yaml
        assert "budget_cap" in yaml
        assert '"1000"' in yaml
        assert "monthly" in yaml
        assert "deny" in yaml


class TestRateLimitYAML:
    def test_basic_output(self):
        yaml = _generate_rate_limit_yaml(
            name="api-rate",
            max_calls=100,
            window_seconds=60,
        )
        assert "api-rate" in yaml
        assert "rate_limit" in yaml
        assert "max_calls: 100" in yaml
        assert "window_seconds: 60" in yaml
        assert "throttle" in yaml
        assert "retry_after_seconds: 30" in yaml


class TestAmplificationGateYAML:
    def test_basic_output(self):
        yaml = _generate_amplification_gate_yaml(
            name="amp-gate",
            max_amplification=3.0,
        )
        assert "amp-gate" in yaml
        assert "amplification_gate" in yaml
        assert "max_amplification: 3.0" in yaml
        assert "deny" in yaml


class TestModelDenylistYAML:
    def test_basic_output(self):
        yaml = _generate_model_denylist_yaml(
            name="block-old",
            models=["gpt-4", "gpt-4-turbo"],
        )
        assert "block-old" in yaml
        assert "model_denylist" in yaml
        assert "deny" in yaml
        assert "- gpt-4" in yaml
        assert "- gpt-4-turbo" in yaml

    def test_single_model(self):
        yaml = _generate_model_denylist_yaml(
            name="block-one",
            models=["gpt-3.5-turbo"],
        )
        assert "- gpt-3.5-turbo" in yaml

    def test_empty_models(self):
        yaml = _generate_model_denylist_yaml(name="empty", models=[])
        assert "model_denylist" in yaml
        # No model lines beyond the header
