"""Tests for orchestrator.core.governance_loop — YAML generation and detection helpers."""
from orchestrator.core.governance_loop import (
    _generate_amplification_gate_yaml,
    _generate_budget_cap_yaml,
    _generate_degradation_ladder_yaml,
    _generate_model_denylist_yaml,
    _generate_rate_limit_yaml,
    _EXPENSIVE_MODELS,
    _CHEAP_ALTERNATIVES,
    GOVERNANCE_DEFAULTS,
    _PROPOSAL_TYPE_TO_RULE,
    _RULE_DETAIL,
)


class TestYamlGenerators:
    def test_degradation_ladder(self):
        yaml = _generate_degradation_ladder_yaml(
            name="test-ladder",
            budget_usd="100.00",
            period="daily",
            tiers=[
                {"pct": 50, "model": "gpt-4o-mini"},
                {"pct": 90, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        )
        assert "test-ladder" in yaml
        assert "degradation_ladder" in yaml
        assert '"100.00"' in yaml
        assert "gpt-4o-mini" in yaml
        assert "deny" in yaml

    def test_budget_cap(self):
        yaml = _generate_budget_cap_yaml(
            name="cap-daily",
            cap_usd="50.00",
            period="daily",
        )
        assert "cap-daily" in yaml
        assert "budget_cap" in yaml
        assert '"50.00"' in yaml
        assert "deny" in yaml

    def test_rate_limit(self):
        yaml = _generate_rate_limit_yaml(
            name="rl-test",
            max_calls=1000,
            window_seconds=3600,
        )
        assert "rl-test" in yaml
        assert "rate_limit" in yaml
        assert "1000" in yaml
        assert "3600" in yaml
        assert "throttle" in yaml

    def test_amplification_gate(self):
        yaml = _generate_amplification_gate_yaml(
            name="amp-gate",
            max_amplification=3.5,
        )
        assert "amp-gate" in yaml
        assert "amplification_gate" in yaml
        assert "3.5" in yaml
        assert "deny" in yaml

    def test_model_denylist(self):
        yaml = _generate_model_denylist_yaml(
            name="deny-models",
            models=["gpt-4", "gpt-4-turbo"],
        )
        assert "deny-models" in yaml
        assert "model_denylist" in yaml
        assert "gpt-4" in yaml
        assert "gpt-4-turbo" in yaml


class TestExpensiveModels:
    def test_expensive_models_not_empty(self):
        assert len(_EXPENSIVE_MODELS) > 0

    def test_cheap_alternatives_cover_most_expensive(self):
        covered = sum(1 for m in _EXPENSIVE_MODELS if m in _CHEAP_ALTERNATIVES)
        # Most expensive models should have alternatives
        assert covered >= len(_EXPENSIVE_MODELS) - 2


class TestGovernanceDefaults:
    def test_defaults_keys(self):
        assert "task.governance_loop.enabled" in GOVERNANCE_DEFAULTS
        assert "governance.lookback_hours" in GOVERNANCE_DEFAULTS
        assert "governance.min_calls_for_analysis" in GOVERNANCE_DEFAULTS

    def test_defaults_are_strings(self):
        for v in GOVERNANCE_DEFAULTS.values():
            assert isinstance(v, str)


class TestProposalTypeToRule:
    def test_known_mappings(self):
        assert _PROPOSAL_TYPE_TO_RULE["model_downshift"] == "model_overprovision"
        assert _PROPOSAL_TYPE_TO_RULE["budget_tighten"] == "budget_overruns"
        assert _PROPOSAL_TYPE_TO_RULE["amplification_gate"] == "amplification_patterns"

    def test_rule_detail_covers_all_rules(self):
        for rule_name in set(_PROPOSAL_TYPE_TO_RULE.values()):
            assert rule_name in _RULE_DETAIL, f"Missing rule detail for {rule_name}"
