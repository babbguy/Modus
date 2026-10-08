"""
Governance Loop & Insights Engine

Unit tests for governance loop YAML helpers, detection rules,
and insights engine setting/explanation helpers.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── YAML generation helpers (governance_loop.py) ─────────────────────────────


def test_generate_degradation_ladder_yaml():
    from orchestrator.core.governance_loop import _generate_degradation_ladder_yaml

    yaml = _generate_degradation_ladder_yaml(
        name="Team Budget Ladder",
        budget_usd="1000",
        period="monthly",
        tiers=[
            {"pct": 80, "model": "gpt-4o-mini"},
            {"pct": 100, "action": "deny"},
        ],
    )
    assert "degradation_ladder" in yaml
    assert "Team Budget Ladder" in yaml
    assert 'budget_usd: "1000"' in yaml
    assert "gpt-4o-mini" in yaml
    assert "action: deny" in yaml


def test_generate_budget_cap_yaml():
    from orchestrator.core.governance_loop import _generate_budget_cap_yaml

    yaml = _generate_budget_cap_yaml(
        name="Monthly Cap",
        cap_usd="5000",
        period="monthly",
    )
    assert "budget_cap" in yaml
    assert "Monthly Cap" in yaml
    assert "5000" in yaml


def test_generate_rate_limit_yaml():
    from orchestrator.core.governance_loop import _generate_rate_limit_yaml

    yaml = _generate_rate_limit_yaml(
        name="Rate Limiter",
        max_calls=1000,
        window_seconds=60,
    )
    assert "rate_limit" in yaml
    assert "max_calls: 1000" in yaml
    assert "window_seconds: 60" in yaml


def test_generate_amplification_gate_yaml():
    from orchestrator.core.governance_loop import _generate_amplification_gate_yaml

    yaml = _generate_amplification_gate_yaml(
        name="Amp Gate",
        max_amplification=5.0,
    )
    assert "amplification_gate" in yaml
    assert "max_amplification: 5.0" in yaml


# ── Expensive model detection ────────────────────────────────────────────────


def test_expensive_models_set():
    from orchestrator.core.governance_loop import _EXPENSIVE_MODELS, _CHEAP_ALTERNATIVES

    assert "gpt-4o" in _EXPENSIVE_MODELS
    assert "gpt-4o-mini" not in _EXPENSIVE_MODELS
    assert _CHEAP_ALTERNATIVES["gpt-4o"] == "gpt-4o-mini"


# ── Governance defaults ──────────────────────────────────────────────────────


def test_governance_defaults():
    from orchestrator.core.governance_loop import GOVERNANCE_DEFAULTS

    assert "task.governance_loop.enabled" in GOVERNANCE_DEFAULTS
    assert "governance.lookback_hours" in GOVERNANCE_DEFAULTS
    assert GOVERNANCE_DEFAULTS["task.governance_loop.interval_seconds"] == "3600"


# ── Proposal type to rule mapping ────────────────────────────────────────────


def test_proposal_type_to_rule():
    from orchestrator.core.governance_loop import _PROPOSAL_TYPE_TO_RULE

    assert _PROPOSAL_TYPE_TO_RULE["model_downshift"] == "model_overprovision"
    assert _PROPOSAL_TYPE_TO_RULE["budget_tighten"] == "budget_overruns"
    assert _PROPOSAL_TYPE_TO_RULE["amplification_gate"] == "amplification_patterns"


def test_rule_detail():
    from orchestrator.core.governance_loop import _RULE_DETAIL

    assert "Expensive model" in _RULE_DETAIL["model_overprovision"]
    assert "Budget cap" in _RULE_DETAIL["budget_overruns"]


# ── Insights engine settings helpers ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_setting_default():
    from orchestrator.core.insights_engine import get_setting

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    result = await get_setting(db, "task.anomaly_scan.enabled")
    assert result == "true"


@pytest.mark.asyncio
async def test_get_setting_from_db():
    from orchestrator.core.insights_engine import get_setting

    mock_setting = MagicMock()
    mock_setting.value = "false"
    db = AsyncMock()
    db.get = AsyncMock(return_value=mock_setting)
    result = await get_setting(db, "task.anomaly_scan.enabled")
    assert result == "false"


@pytest.mark.asyncio
async def test_get_setting_int():
    from orchestrator.core.insights_engine import get_setting_int

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    result = await get_setting_int(db, "task.anomaly_scan.interval_seconds", 300)
    assert result == 300


@pytest.mark.asyncio
async def test_get_setting_int_from_db():
    from orchestrator.core.insights_engine import get_setting_int

    mock_setting = MagicMock()
    mock_setting.value = "600"
    db = AsyncMock()
    db.get = AsyncMock(return_value=mock_setting)
    result = await get_setting_int(db, "task.anomaly_scan.interval_seconds")
    assert result == 600


@pytest.mark.asyncio
async def test_get_setting_float():
    from orchestrator.core.insights_engine import get_setting_float

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    result = await get_setting_float(db, "insights.anomaly.zscore_threshold", 3.0)
    assert result == 3.0


@pytest.mark.asyncio
async def test_get_setting_bool():
    from orchestrator.core.insights_engine import get_setting_bool

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    result = await get_setting_bool(db, "task.anomaly_scan.enabled", True)
    assert result is True


@pytest.mark.asyncio
async def test_get_setting_bool_false():
    from orchestrator.core.insights_engine import get_setting_bool

    mock_setting = MagicMock()
    mock_setting.value = "false"
    db = AsyncMock()
    db.get = AsyncMock(return_value=mock_setting)
    result = await get_setting_bool(db, "task.anomaly_scan.enabled")
    assert result is False


# ── Fallback explanation ─────────────────────────────────────────────────────


def test_fallback_explain_anomaly():
    from orchestrator.core.insights_engine import _fallback_explain

    result = _fallback_explain("anomaly detected with z-score 4.2")
    assert "anomaly" in result.lower()


def test_fallback_explain_recommendation():
    from orchestrator.core.insights_engine import _fallback_explain

    result = _fallback_explain("recommend switching to cheaper model")
    assert "switching" in result.lower() or "cheaper" in result.lower()


def test_fallback_explain_generic():
    from orchestrator.core.insights_engine import _fallback_explain

    result = _fallback_explain("some other context")
    assert "Analysis generated" in result


# ── Defaults dict ────────────────────────────────────────────────────────────


def test_insights_defaults():
    from orchestrator.core.insights_engine import _DEFAULTS

    assert "task.anomaly_scan.enabled" in _DEFAULTS
    assert "task.forecast.enabled" in _DEFAULTS
    assert "task.recommendations.enabled" in _DEFAULTS
    assert _DEFAULTS["task.anomaly_scan.interval_seconds"] == "300"


# ── run_anomaly_scan skips on SQLite ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_anomaly_scan_skips_sqlite():
    from orchestrator.core.insights_engine import run_anomaly_scan

    # In test env, database is SQLite, so scan should exit early
    await run_anomaly_scan()  # Should not raise


# ── run_forecast_update ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_forecast_update_no_factory():
    from orchestrator.core.insights_engine import run_forecast_update
    import orchestrator.core.insights_engine as ie

    old = ie._session_factory
    ie._session_factory = None
    await run_forecast_update()  # Should exit early
    ie._session_factory = old


# ── run_recommendation_refresh ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_recommendation_refresh_no_factory():
    from orchestrator.core.insights_engine import run_recommendation_refresh
    import orchestrator.core.insights_engine as ie

    old = ie._session_factory
    ie._session_factory = None
    await run_recommendation_refresh()  # Should exit early
    ie._session_factory = old
