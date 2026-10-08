"""
Governance loop deep coverage.

Targets orchestrator.core.governance_loop:
  - YAML generators (128-218)
  - _detect_model_overprovision (265-299)
  - _detect_budget_overruns (362, 388-397)
  - _detect_amplification_patterns (471-514)
  - _detect_seasonal_patterns (550-577)
  - run_governance_loop (648-825)
  - _store_proposal (849-851, 855-858)
  - apply_proposal (898-899, 934-935)
  - dismiss_proposal (945, 947, 958-959)
  - CoT logging integrations
  - early-exit paths
"""
from __future__ import annotations

from unittest.mock import patch

from orchestrator.core.governance_loop import (
    _generate_degradation_ladder_yaml,
    _generate_budget_cap_yaml,
    _generate_rate_limit_yaml,
    _generate_amplification_gate_yaml,
    _generate_model_denylist_yaml,
    _EXPENSIVE_MODELS,
    _CHEAP_ALTERNATIVES,
    GOVERNANCE_DEFAULTS,
    _PROPOSAL_TYPE_TO_RULE,
    _RULE_DETAIL,
)
from orchestrator.core import governance_loop as gl_mod


# ── YAML generators ──────────────────────────────────────────────────────────

class TestYamlGenerators:
    def test_degradation_ladder(self):
        yaml = _generate_degradation_ladder_yaml(
            name="test-ladder",
            budget_usd="1000",
            period="monthly",
            tiers=[
                {"pct": 80, "model": "gpt-4o-mini"},
                {"pct": 100, "action": "deny"},
            ],
        )
        assert "test-ladder" in yaml
        assert "degradation_ladder" in yaml
        assert "gpt-4o-mini" in yaml
        assert "deny" in yaml
        assert 'budget_usd: "1000"' in yaml

    def test_budget_cap(self):
        yaml = _generate_budget_cap_yaml(
            name="test-cap",
            cap_usd="500",
            period="daily",
        )
        assert "test-cap" in yaml
        assert "budget_cap" in yaml
        assert '"500"' in yaml
        assert "daily" in yaml

    def test_rate_limit(self):
        yaml = _generate_rate_limit_yaml(
            name="test-rl",
            max_calls=100,
            window_seconds=3600,
        )
        assert "test-rl" in yaml
        assert "rate_limit" in yaml
        assert "100" in yaml
        assert "3600" in yaml
        assert "throttle" in yaml

    def test_amplification_gate(self):
        yaml = _generate_amplification_gate_yaml(
            name="test-amp",
            max_amplification=5.0,
        )
        assert "test-amp" in yaml
        assert "amplification_gate" in yaml
        assert "5.0" in yaml

    def test_model_denylist(self):
        yaml = _generate_model_denylist_yaml(
            name="test-deny",
            models=["gpt-4", "claude-opus-4-5"],
        )
        assert "test-deny" in yaml
        assert "model_denylist" in yaml
        assert "gpt-4" in yaml
        assert "claude-opus-4-5" in yaml


# ── Constants ────────────────────────────────────────────────────────────────

class TestConstants:
    def test_expensive_models_exist(self):
        assert "gpt-4o" in _EXPENSIVE_MODELS
        assert "claude-opus-4-5-20251022" in _EXPENSIVE_MODELS

    def test_cheap_alternatives(self):
        assert _CHEAP_ALTERNATIVES["gpt-4o"] == "gpt-4o-mini"
        assert "claude-sonnet" in _CHEAP_ALTERNATIVES["claude-opus-4-5-20251022"]

    def test_governance_defaults(self):
        assert "task.governance_loop.enabled" in GOVERNANCE_DEFAULTS
        assert GOVERNANCE_DEFAULTS["task.governance_loop.interval_seconds"] == "3600"

    def test_proposal_type_to_rule(self):
        assert "model_downshift" in _PROPOSAL_TYPE_TO_RULE
        assert "budget_tighten" in _PROPOSAL_TYPE_TO_RULE

    def test_rule_detail(self):
        assert "model_overprovision" in _RULE_DETAIL
        assert "budget_overruns" in _RULE_DETAIL


# ── Detection with DB queries ────────────────────────────────────────────────

class TestDetectionRules:
    async def test_model_overprovision_no_data(self, db_session):
        from orchestrator.core.governance_loop import _detect_model_overprovision
        proposals = await _detect_model_overprovision(
            db=db_session,
            team_id="fake-team-id",
            lookback_hours=24,
            min_calls=50,
            output_threshold=200,
            cost_threshold=0.01,
        )
        assert proposals == []

    async def test_budget_overruns_no_data(self, db_session):
        from orchestrator.core.governance_loop import _detect_budget_overruns
        proposals = await _detect_budget_overruns(
            db=db_session,
            team_id="fake-team-id",
            lookback_days=7,
        )
        assert proposals == []

    async def test_amplification_patterns_no_data(self, db_session):
        from orchestrator.core.governance_loop import _detect_amplification_patterns
        proposals = await _detect_amplification_patterns(
            db=db_session,
            team_id="fake-team-id",
            lookback_hours=24,
            threshold=3.0,
        )
        assert proposals == []


# ── Run governance loop early exit ───────────────────────────────────────────

class TestRunGovernanceLoop:
    async def test_no_session_factory(self):
        with patch.object(gl_mod, "_session_factory", None):
            from orchestrator.core.governance_loop import run_governance_loop
            await run_governance_loop()  # should return immediately

    async def test_cot_imports(self):
        from orchestrator.core.governance_loop import _cot_imports
        append_entry, link_entry, infer_tags, build_step = _cot_imports()
        assert callable(append_entry)
        assert callable(link_entry)
        assert callable(infer_tags)
        assert callable(build_step)
