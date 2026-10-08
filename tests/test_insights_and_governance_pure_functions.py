"""
Core module pure functions and helpers.

Targets:
  - orchestrator.core.insights_engine (settings helpers, fallback explain, OLS)
  - orchestrator.core.governance_loop (YAML generators, model tiers, constants)
  - orchestrator.core.report_scheduler (_is_due, _to_csv, _format_slack_message)
  - orchestrator.core.threshold_evaluator (_period_window, _severity_meets_min)
  - orchestrator.core.connection_checker (mask functions, cache, sanitize)
  - orchestrator.core.maintenance (prune, compact helpers)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# ═══════════════════════════════════════════════════════════════════════════════
# 1. INSIGHTS ENGINE — Settings helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def test_get_setting_default(db_session):
    from orchestrator.core.insights_engine import get_setting
    val = await get_setting(db_session, "insights.anomaly.zscore_threshold")
    assert val == "3.0"


async def test_get_setting_int_default(db_session):
    from orchestrator.core.insights_engine import get_setting_int
    val = await get_setting_int(db_session, "insights.anomaly.baseline_days", 14)
    assert val == 14


async def test_get_setting_int_with_value(db_session):
    from orchestrator.db.models import SystemSetting
    db_session.add(SystemSetting(key="test.int.key", value="42"))
    await db_session.flush()
    from orchestrator.core.insights_engine import get_setting_int
    val = await get_setting_int(db_session, "test.int.key", 0)
    assert val == 42


async def test_get_setting_int_bad_value(db_session):
    from orchestrator.db.models import SystemSetting
    db_session.add(SystemSetting(key="test.bad.int", value="not-a-number"))
    await db_session.flush()
    from orchestrator.core.insights_engine import get_setting_int
    val = await get_setting_int(db_session, "test.bad.int", 99)
    assert val == 99


async def test_get_setting_float_default(db_session):
    from orchestrator.core.insights_engine import get_setting_float
    val = await get_setting_float(db_session, "nonexistent.key", 3.14)
    assert val == 3.14


async def test_get_setting_float_with_value(db_session):
    from orchestrator.db.models import SystemSetting
    db_session.add(SystemSetting(key="test.float.key", value="2.718"))
    await db_session.flush()
    from orchestrator.core.insights_engine import get_setting_float
    val = await get_setting_float(db_session, "test.float.key", 0.0)
    assert abs(val - 2.718) < 0.001


async def test_get_setting_bool_true(db_session):
    from orchestrator.db.models import SystemSetting
    db_session.add(SystemSetting(key="test.bool.true", value="true"))
    await db_session.flush()
    from orchestrator.core.insights_engine import get_setting_bool
    val = await get_setting_bool(db_session, "test.bool.true")
    assert val is True


async def test_get_setting_bool_false(db_session):
    from orchestrator.db.models import SystemSetting
    db_session.add(SystemSetting(key="test.bool.false", value="false"))
    await db_session.flush()
    from orchestrator.core.insights_engine import get_setting_bool
    val = await get_setting_bool(db_session, "test.bool.false")
    assert val is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. INSIGHTS ENGINE — Fallback explain
# ═══════════════════════════════════════════════════════════════════════════════

def test_fallback_explain_anomaly():
    from orchestrator.core.insights_engine import _fallback_explain
    result = _fallback_explain("cost anomaly detected with z-score 5.2")
    assert "anomaly" in result.lower() or "spend" in result.lower()


def test_fallback_explain_recommend():
    from orchestrator.core.insights_engine import _fallback_explain
    result = _fallback_explain("recommend switching model to cheaper option")
    assert "model" in result.lower() or "switch" in result.lower()


def test_fallback_explain_generic():
    from orchestrator.core.insights_engine import _fallback_explain
    result = _fallback_explain("some generic context about usage")
    assert "Claude API" in result or "generated" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. INSIGHTS ENGINE — OLS forecast
# ═══════════════════════════════════════════════════════════════════════════════

def test_ols_forecast_linear():
    from orchestrator.core.insights_engine import _ols_forecast
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]  # y = 2x
    slope, intercept, r_squared = _ols_forecast(xs, ys)
    assert abs(slope - 2.0) < 0.01
    assert abs(intercept) < 0.01
    assert r_squared > 0.99


def test_ols_forecast_constant():
    from orchestrator.core.insights_engine import _ols_forecast
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [5.0, 5.0, 5.0, 5.0]
    slope, intercept, r_squared = _ols_forecast(xs, ys)
    assert abs(slope) < 0.01
    assert abs(intercept - 5.0) < 0.01


def test_ols_forecast_single_point():
    from orchestrator.core.insights_engine import _ols_forecast
    xs = [1.0]
    ys = [3.0]
    slope, intercept, r_squared = _ols_forecast(xs, ys)
    # With single point, slope should be 0
    assert abs(slope) < 0.01


# ═══════════════════════════════════════════════════════════════════════════════
# 4. GOVERNANCE LOOP — YAML generators
# ═══════════════════════════════════════════════════════════════════════════════

def test_generate_degradation_ladder_yaml():
    from orchestrator.core.governance_loop import _generate_degradation_ladder_yaml
    yaml_str = _generate_degradation_ladder_yaml(
        name="Test Ladder",
        budget_usd="500",
        period="monthly",
        tiers=[
            {"pct": 80, "model": "gpt-4o-mini"},
            {"pct": 100, "action": "deny"},
        ],
    )
    assert "degradation_ladder" in yaml_str
    assert "gpt-4o-mini" in yaml_str
    assert "deny" in yaml_str


def test_generate_budget_cap_yaml():
    from orchestrator.core.governance_loop import _generate_budget_cap_yaml
    yaml_str = _generate_budget_cap_yaml("Cap Policy", "1000", "monthly")
    assert "budget_cap" in yaml_str
    assert "1000" in yaml_str
    assert "monthly" in yaml_str


def test_generate_rate_limit_yaml():
    from orchestrator.core.governance_loop import _generate_rate_limit_yaml
    yaml_str = _generate_rate_limit_yaml("RL Policy", 100, 3600)
    assert "rate_limit" in yaml_str
    assert "100" in yaml_str
    assert "3600" in yaml_str
    assert "retry_after_seconds" in yaml_str


def test_generate_amplification_gate_yaml():
    from orchestrator.core.governance_loop import _generate_amplification_gate_yaml
    yaml_str = _generate_amplification_gate_yaml("Amp Gate", 5.0)
    assert "amplification_gate" in yaml_str
    assert "5.0" in yaml_str


def test_generate_model_denylist_yaml():
    from orchestrator.core.governance_loop import _generate_model_denylist_yaml
    yaml_str = _generate_model_denylist_yaml(
        "Block Expensive", ["gpt-4-turbo", "claude-opus-4-5-20251022"]
    )
    assert "model_denylist" in yaml_str
    assert "gpt-4-turbo" in yaml_str


# ═══════════════════════════════════════════════════════════════════════════════
# 5. GOVERNANCE LOOP — Constants and maps
# ═══════════════════════════════════════════════════════════════════════════════

def test_expensive_models_set():
    from orchestrator.core.governance_loop import _EXPENSIVE_MODELS
    assert "gpt-4o" in _EXPENSIVE_MODELS
    assert "claude-opus-4-5-20251022" in _EXPENSIVE_MODELS
    assert "gpt-4o-mini" not in _EXPENSIVE_MODELS


def test_cheap_alternatives_map():
    from orchestrator.core.governance_loop import _CHEAP_ALTERNATIVES
    assert _CHEAP_ALTERNATIVES["gpt-4o"] == "gpt-4o-mini"
    assert _CHEAP_ALTERNATIVES["claude-opus-4-5-20251022"] == "claude-sonnet-4-5"
    # Current Anthropic families must have a cheaper alternative, or the
    # overprovision detector flags a model it cannot propose a downshift for.
    assert _CHEAP_ALTERNATIVES["claude-opus-5"] == "claude-sonnet-5"
    assert _CHEAP_ALTERNATIVES["claude-fable-5"] == "claude-opus-5"
    from orchestrator.core.governance_loop import _EXPENSIVE_MODELS
    assert set(_EXPENSIVE_MODELS) <= set(_CHEAP_ALTERNATIVES)


def test_proposal_type_to_rule():
    from orchestrator.core.governance_loop import _PROPOSAL_TYPE_TO_RULE
    assert _PROPOSAL_TYPE_TO_RULE["model_downshift"] == "model_overprovision"
    assert _PROPOSAL_TYPE_TO_RULE["budget_tighten"] == "budget_overruns"


def test_rule_detail():
    from orchestrator.core.governance_loop import _RULE_DETAIL
    assert "Expensive model" in _RULE_DETAIL["model_overprovision"]
    assert "Budget cap" in _RULE_DETAIL["budget_overruns"]


# ═══════════════════════════════════════════════════════════════════════════════
# 6. GOVERNANCE LOOP — PQC YAML generator
# ═══════════════════════════════════════════════════════════════════════════════

def test_generate_pqc_migration_yaml():
    from orchestrator.core.governance_loop import _generate_pqc_migration_yaml
    yaml_str = _generate_pqc_migration_yaml("hybrid", "ML-KEM-768")
    assert "pqc_migration" in yaml_str
    assert "hybrid" in yaml_str
    assert "ML-KEM-768" in yaml_str


# ═══════════════════════════════════════════════════════════════════════════════
# 7. REPORT SCHEDULER — _is_due
# ═══════════════════════════════════════════════════════════════════════════════

def _make_report(schedule: str, last_run_at=None, is_active=True):
    """Create a minimal mock FinanceReport."""
    return SimpleNamespace(
        schedule=schedule,
        last_run_at=last_run_at,
        is_active=is_active,
    )


def test_is_due_never_run():
    from orchestrator.core.report_scheduler import _is_due
    report = _make_report("daily", last_run_at=None)
    now = datetime.now(timezone.utc)
    assert _is_due(report, now) is True


def test_is_due_inactive():
    from orchestrator.core.report_scheduler import _is_due
    report = _make_report("daily", is_active=False)
    now = datetime.now(timezone.utc)
    assert _is_due(report, now) is False


def test_is_due_daily_already_run_today():
    from orchestrator.core.report_scheduler import _is_due
    # Fixed mid-day timestamp: now-relative math crosses the UTC date
    # boundary when the suite runs near midnight, making a daily report
    # legitimately due and the test flaky.
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    report = _make_report("daily", last_run_at=now - timedelta(hours=1))
    assert _is_due(report, now) is False


def test_is_due_daily_run_yesterday():
    from orchestrator.core.report_scheduler import _is_due
    now = datetime.now(timezone.utc)
    report = _make_report("daily", last_run_at=now - timedelta(days=1))
    assert _is_due(report, now) is True


def test_is_due_weekly_already_run_this_week():
    from orchestrator.core.report_scheduler import _is_due
    now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)  # Thursday
    monday = now - timedelta(days=now.weekday())  # This Monday
    report = _make_report("weekly", last_run_at=monday + timedelta(hours=6))
    assert _is_due(report, now) is False


def test_is_due_weekly_run_last_week():
    from orchestrator.core.report_scheduler import _is_due
    now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)  # Thursday
    report = _make_report("weekly", last_run_at=now - timedelta(days=8))
    assert _is_due(report, now) is True


def test_is_due_monthly():
    from orchestrator.core.report_scheduler import _is_due
    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    report = _make_report("monthly", last_run_at=datetime(2026, 3, 20, tzinfo=timezone.utc))
    assert _is_due(report, now) is True


def test_is_due_monthly_already_run():
    from orchestrator.core.report_scheduler import _is_due
    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    report = _make_report("monthly", last_run_at=datetime(2026, 4, 2, tzinfo=timezone.utc))
    assert _is_due(report, now) is False


def test_is_due_quarterly():
    from orchestrator.core.report_scheduler import _is_due
    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)  # Q2
    report = _make_report("quarterly", last_run_at=datetime(2026, 3, 15, tzinfo=timezone.utc))
    assert _is_due(report, now) is True


def test_is_due_quarterly_already_run():
    from orchestrator.core.report_scheduler import _is_due
    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    report = _make_report("quarterly", last_run_at=datetime(2026, 4, 5, tzinfo=timezone.utc))
    assert _is_due(report, now) is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. REPORT SCHEDULER — _to_csv
# ═══════════════════════════════════════════════════════════════════════════════

def test_to_csv_empty():
    from orchestrator.core.report_scheduler import _to_csv
    assert _to_csv([]) == ""


def test_to_csv_with_data():
    from orchestrator.core.report_scheduler import _to_csv
    data = [
        {"team": "alpha", "cost": 100.5},
        {"team": "beta", "cost": 200.0},
    ]
    csv_str = _to_csv(data)
    assert "team" in csv_str
    assert "cost" in csv_str
    assert "alpha" in csv_str
    assert "beta" in csv_str
    lines = csv_str.strip().split("\n")
    assert len(lines) == 3  # header + 2 rows


# ═══════════════════════════════════════════════════════════════════════════════
# 9. REPORT SCHEDULER — _format_slack_message
# ═══════════════════════════════════════════════════════════════════════════════

def test_format_slack_message_burn_rate():
    from orchestrator.core.report_scheduler import _format_slack_message
    report = SimpleNamespace(
        name="Weekly Burn Rate",
        report_type="burn_rate",
        schedule="weekly",
    )
    data = [
        {"team_name": "Alpha", "spend_usd": 500, "budget_usd": 1000,
         "burn_pct": 50, "risk": "on-track"},
        {"team_name": "Beta", "spend_usd": 900, "budget_usd": 1000,
         "burn_pct": 90, "risk": "at-risk"},
    ]
    now = datetime.now(timezone.utc)
    msg = _format_slack_message(report, data, now)
    assert "blocks" in msg
    assert len(msg["blocks"]) >= 2


def test_format_slack_message_forecast():
    from orchestrator.core.report_scheduler import _format_slack_message
    report = SimpleNamespace(
        name="Weekly Forecast",
        report_type="forecast",
        schedule="weekly",
    )
    data = [
        {"team_name": "Alpha", "forecast_eom_usd": 1500,
         "breach_predicted": True, "breach_date": "2026-04-25"},
        {"team_name": "Beta", "forecast_eom_usd": 800,
         "breach_predicted": False},
    ]
    now = datetime.now(timezone.utc)
    msg = _format_slack_message(report, data, now)
    assert "blocks" in msg


def test_format_slack_message_variance():
    from orchestrator.core.report_scheduler import _format_slack_message
    report = SimpleNamespace(
        name="Weekly Variance",
        report_type="variance",
        schedule="weekly",
    )
    data = [
        {"team_name": "Alpha", "delta_usd": 200, "delta_pct": 15.5},
        {"team_name": "Beta", "delta_usd": -100, "delta_pct": -8.0},
    ]
    now = datetime.now(timezone.utc)
    msg = _format_slack_message(report, data, now)
    assert "blocks" in msg


def test_format_slack_message_generic():
    from orchestrator.core.report_scheduler import _format_slack_message
    report = SimpleNamespace(
        name="Custom Report",
        report_type="chargeback",
        schedule="monthly",
    )
    data = [{"cost_usd": 500}, {"cost_usd": 300}]
    now = datetime.now(timezone.utc)
    msg = _format_slack_message(report, data, now)
    assert "blocks" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# 10. THRESHOLD EVALUATOR — _period_window
# ═══════════════════════════════════════════════════════════════════════════════

def test_period_window_hourly():
    from orchestrator.core.threshold_evaluator import _period_window
    now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
    start, end = _period_window("hourly", now)
    assert start == datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 9, 15, 0, 0, tzinfo=timezone.utc)


def test_period_window_daily():
    from orchestrator.core.threshold_evaluator import _period_window
    now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
    start, end = _period_window("daily", now)
    assert start == datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_period_window_weekly():
    from orchestrator.core.threshold_evaluator import _period_window
    now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)  # Thursday
    start, end = _period_window("weekly", now)
    assert start.weekday() == 0  # Monday
    assert (end - start).days == 7


def test_period_window_monthly():
    from orchestrator.core.threshold_evaluator import _period_window
    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    start, end = _period_window("monthly", now)
    assert start == datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_period_window_monthly_december():
    from orchestrator.core.threshold_evaluator import _period_window
    now = datetime(2026, 12, 15, 12, 0, 0, tzinfo=timezone.utc)
    start, end = _period_window("monthly", now)
    assert start == datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. THRESHOLD EVALUATOR — _severity_meets_min
# ═══════════════════════════════════════════════════════════════════════════════

def test_severity_meets_min():
    from orchestrator.core.threshold_evaluator import _severity_meets_min
    assert _severity_meets_min("critical", "caution") is True
    assert _severity_meets_min("critical", "warning") is True
    assert _severity_meets_min("critical", "critical") is True
    assert _severity_meets_min("warning", "critical") is False
    assert _severity_meets_min("caution", "critical") is False
    assert _severity_meets_min("caution", "caution") is True
    assert _severity_meets_min("warning", "warning") is True


def test_severity_unknown_defaults_to_zero():
    from orchestrator.core.threshold_evaluator import _severity_meets_min
    assert _severity_meets_min("unknown", "caution") is True
    assert _severity_meets_min("unknown", "warning") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 12. THRESHOLD EVALUATOR — _fired_tier_key
# ═══════════════════════════════════════════════════════════════════════════════

def test_fired_tier_key():
    from orchestrator.core.threshold_evaluator import _fired_tier_key
    tid = "abc123"
    ps = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
    key = _fired_tier_key(tid, ps)
    assert isinstance(key, tuple)
    assert len(key) == 2
    assert key[0] == "abc123"


# ═══════════════════════════════════════════════════════════════════════════════
# 13. CONNECTION CHECKER — masking
# ═══════════════════════════════════════════════════════════════════════════════

def test_mask_url_normal():
    from orchestrator.core.connection_checker import _mask_url
    result = _mask_url("https://api.anthropic.com/v1/messages")
    assert "***" in result
    assert "api.anth" in result
    assert result.startswith("https://")


def test_mask_url_short_host():
    from orchestrator.core.connection_checker import _mask_url
    result = _mask_url("http://local")
    assert "***" in result


def test_mask_url_empty():
    from orchestrator.core.connection_checker import _mask_url
    assert _mask_url("") == ""


def test_mask_key_normal():
    from orchestrator.core.connection_checker import _mask_key
    result = _mask_key("sk-1234567890abcdef")
    assert result == "sk-...cdef"


def test_mask_key_short():
    from orchestrator.core.connection_checker import _mask_key
    result = _mask_key("short")
    assert result == "sh..."


def test_mask_key_empty():
    from orchestrator.core.connection_checker import _mask_key
    assert _mask_key("") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 14. CONNECTION CHECKER — cache
# ═══════════════════════════════════════════════════════════════════════════════

def test_cache_set_and_get():
    from orchestrator.core.connection_checker import (
        _cache_set, _cache_get, clear_cache,
    )
    clear_cache()
    _cache_set("conn-1", {"status": "ok"})
    result = _cache_get("conn-1")
    assert result == {"status": "ok"}


def test_cache_miss():
    from orchestrator.core.connection_checker import _cache_get, clear_cache
    clear_cache()
    assert _cache_get("nonexistent") is None


def test_cache_clear():
    from orchestrator.core.connection_checker import (
        _cache_set, _cache_get, clear_cache,
    )
    _cache_set("conn-2", {"status": "ok"})
    clear_cache()
    assert _cache_get("conn-2") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 15. CONNECTION CHECKER — sanitize_connection
# ═══════════════════════════════════════════════════════════════════════════════

def test_sanitize_connection():
    from orchestrator.core.connection_checker import sanitize_connection
    conn = {
        "id": "test-conn",
        "name": "OpenAI",
        "url": "https://api.openai.com",
        "api_key": "sk-proj-1234567890abcdef",
        "status": "connected",
        "category": "ai_provider",
    }
    safe = sanitize_connection(conn)
    assert safe["id"] == "test-conn"
    # Original should not be mutated
    assert conn["api_key"] == "sk-proj-1234567890abcdef"


# ═══════════════════════════════════════════════════════════════════════════════
# 16. CONNECTION CHECKER — _check_url
# ═══════════════════════════════════════════════════════════════════════════════

def test_check_url_invalid_host():
    from orchestrator.core.connection_checker import _check_url
    ok, status, error = _check_url(
        "http://nonexistent.invalid.host.example", timeout=2,
    )
    assert ok is False
    assert status == 0
    assert error is not None


def test_make_connection():
    from orchestrator.core.connection_checker import _make_connection
    conn = _make_connection(
        connection_id="test-1",
        name="Test Connection",
        category="ai_provider",
        conn_type="http",
        endpoint="https://api.example.com",
    )
    assert conn["id"] == "test-1"
    assert conn["name"] == "Test Connection"
    assert conn["category"] == "ai_provider"
    assert conn["type"] == "http"
    assert "***" in conn["endpoint"]  # masked
