"""
Insights Engine coverage.

Targets orchestrator.core.insights_engine: settings helpers, fallback
explanations, anomaly scan, recommendation refresh, spend forecast.
"""
from __future__ import annotations

from unittest.mock import patch

# ═══════════════════════════════════════════════════════════════════════════════
# 1. SETTINGS HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_get_setting_default(db_session):
    from orchestrator.core.insights_engine import get_setting
    val = await get_setting(db_session, "task.anomaly_scan.enabled")
    assert val == "true"


async def test_get_setting_missing(db_session):
    from orchestrator.core.insights_engine import get_setting
    val = await get_setting(db_session, "nonexistent.key.here")
    assert val == ""


async def test_get_setting_int(db_session):
    from orchestrator.core.insights_engine import get_setting_int
    val = await get_setting_int(db_session, "task.anomaly_scan.interval_seconds")
    assert val == 300


async def test_get_setting_int_bad_value(db_session):
    from orchestrator.core.insights_engine import get_setting_int
    val = await get_setting_int(db_session, "nonexistent", default=42)
    assert val == 42


async def test_get_setting_float(db_session):
    from orchestrator.core.insights_engine import get_setting_float
    val = await get_setting_float(db_session, "insights.anomaly.zscore_threshold")
    assert val == 3.0


async def test_get_setting_float_bad_value(db_session):
    from orchestrator.core.insights_engine import get_setting_float
    val = await get_setting_float(db_session, "nonexistent", default=1.5)
    assert val == 1.5


async def test_get_setting_bool_true(db_session):
    from orchestrator.core.insights_engine import get_setting_bool
    val = await get_setting_bool(db_session, "task.anomaly_scan.enabled")
    assert val is True


async def test_get_setting_bool_default(db_session):
    from orchestrator.core.insights_engine import get_setting_bool
    val = await get_setting_bool(db_session, "nonexistent.key", default=False)
    # Empty string -> not in ("true", "1", "yes", "on") -> False
    assert val is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. FALLBACK EXPLAIN
# ═══════════════════════════════════════════════════════════════════════════════

def test_fallback_explain_anomaly():
    from orchestrator.core.insights_engine import _fallback_explain
    result = _fallback_explain("Cost anomaly detected with z-score of 4.5")
    assert "anomaly" in result.lower()


def test_fallback_explain_recommend():
    from orchestrator.core.insights_engine import _fallback_explain
    result = _fallback_explain("Consider switching to a cheaper model")
    assert "switch" in result.lower() or "cheaper" in result.lower()


def test_fallback_explain_generic():
    from orchestrator.core.insights_engine import _fallback_explain
    result = _fallback_explain("Some unrelated context")
    assert "AI" in result or "enable" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. AI EXPLAIN (mocked)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_ai_explain_with_api_success():
    from orchestrator.core.insights_engine import _ai_explain
    with patch(
        "orchestrator.core.insights_engine._anthropic_api_key", return_value="sk-test"
    ), patch(
        "orchestrator.core.insights_engine._claude_generate",
        return_value="AI generated explanation",
    ):
        result = await _ai_explain("system", "user")
        assert result == "AI generated explanation"


async def test_ai_explain_fallback_on_failure():
    from orchestrator.core.insights_engine import _ai_explain
    with patch(
        "orchestrator.core.insights_engine._claude_generate",
        return_value=None,
    ):
        result = await _ai_explain("system", "anomaly context")
        assert result is not None
        assert len(result) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ANOMALY SCAN (SQLite → skipped)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_anomaly_scan_skips_sqlite():
    """Anomaly scan requires PostgreSQL — should be a no-op on SQLite."""
    from orchestrator.core.insights_engine import run_anomaly_scan
    # Should not raise
    await run_anomaly_scan()


async def test_anomaly_scan_no_session_factory():
    from orchestrator.core import insights_engine as mod
    original = mod._session_factory
    try:
        mod._session_factory = None
        await mod.run_anomaly_scan()  # should be a no-op
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 5. RECOMMENDATION REFRESH (SQLite → skipped)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_recommendation_refresh_skips():
    """Recommendation refresh should handle gracefully on SQLite."""
    from orchestrator.core.insights_engine import run_recommendation_refresh
    # Should not raise even on SQLite
    await run_recommendation_refresh()


async def test_recommendation_refresh_no_session_factory():
    from orchestrator.core import insights_engine as mod
    original = mod._session_factory
    try:
        mod._session_factory = None
        await mod.run_recommendation_refresh()
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 6. SPEND FORECAST (SQLite → skipped)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_forecast_update_skips():
    """Forecast update should handle gracefully on SQLite."""
    from orchestrator.core.insights_engine import run_forecast_update
    await run_forecast_update()


async def test_forecast_update_no_session_factory():
    from orchestrator.core import insights_engine as mod
    original = mod._session_factory
    try:
        mod._session_factory = None
        await mod.run_forecast_update()
    finally:
        mod._session_factory = original
