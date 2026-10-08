"""
Tests for orchestrator.core.insights_engine — Settings helpers, OLS forecast,
fallback explanations, and Claude API helper.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.core.insights_engine import (
    _fallback_explain,
    _ols_forecast,
    get_setting,
    get_setting_bool,
    get_setting_float,
    get_setting_int,
    _claude_generate,
)
from orchestrator.db.models import Base, SystemSetting


@pytest_asyncio.fixture
async def insights_db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await eng.dispose()
# ── Settings helpers ─────────────────────────────────────────────────────────


class TestGetSetting:
    async def test_returns_default_when_no_row(self, insights_db):
        result = await get_setting(insights_db, "task.anomaly_scan.enabled")
        assert result == "true"

    async def test_returns_custom_value(self, insights_db):
        insights_db.add(SystemSetting(key="task.anomaly_scan.enabled", value="false"))
        await insights_db.flush()
        result = await get_setting(insights_db, "task.anomaly_scan.enabled")
        assert result == "false"

    async def test_unknown_key_returns_empty(self, insights_db):
        result = await get_setting(insights_db, "nonexistent.key")
        assert result == ""


class TestGetSettingInt:
    async def test_valid_int(self, insights_db):
        result = await get_setting_int(insights_db, "task.anomaly_scan.interval_seconds")
        assert result == 300

    async def test_invalid_returns_default(self, insights_db):
        insights_db.add(SystemSetting(key="bad.int", value="notanumber"))
        await insights_db.flush()
        result = await get_setting_int(insights_db, "bad.int", default=42)
        assert result == 42

    async def test_empty_returns_default(self, insights_db):
        result = await get_setting_int(insights_db, "nonexistent", default=7)
        assert result == 7


class TestGetSettingFloat:
    async def test_valid_float(self, insights_db):
        result = await get_setting_float(insights_db, "insights.anomaly.zscore_threshold")
        assert result == 3.0

    async def test_invalid_returns_default(self, insights_db):
        insights_db.add(SystemSetting(key="bad.float", value="abc"))
        await insights_db.flush()
        result = await get_setting_float(insights_db, "bad.float", default=1.5)
        assert result == 1.5


class TestGetSettingBool:
    async def test_true_values(self, insights_db):
        for val in ("true", "1", "yes", "on"):
            insights_db.add(SystemSetting(key=f"bool.{val}", value=val))
        await insights_db.flush()
        for val in ("true", "1", "yes", "on"):
            result = await get_setting_bool(insights_db, f"bool.{val}")
            assert result is True

    async def test_false_values(self, insights_db):
        insights_db.add(SystemSetting(key="bool.false", value="false"))
        await insights_db.flush()
        result = await get_setting_bool(insights_db, "bool.false")
        assert result is False

    async def test_default_from_defaults(self, insights_db):
        result = await get_setting_bool(insights_db, "task.anomaly_scan.enabled")
        assert result is True


# ── OLS Forecast ─────────────────────────────────────────────────────────────


class TestOLSForecast:
    def test_two_points(self):
        slope, intercept, r_sq = _ols_forecast([0.0, 1.0], [100.0, 200.0])
        assert abs(slope - 100.0) < 0.01
        assert abs(intercept - 100.0) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_single_point(self):
        slope, intercept, r_sq = _ols_forecast([0.0], [50.0])
        assert slope == 0.0
        assert intercept == 50.0
        assert r_sq == 0.0

    def test_flat_line(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [100.0, 100.0, 100.0, 100.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert abs(slope) < 0.01
        assert abs(intercept - 100.0) < 0.01

    def test_perfect_positive_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [10.0, 20.0, 30.0, 40.0, 50.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert abs(slope - 10.0) < 0.01
        assert abs(intercept) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_negative_slope(self):
        xs = [1.0, 2.0, 3.0]
        ys = [300.0, 200.0, 100.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert slope < 0
        assert abs(r_sq - 1.0) < 0.01

    def test_r_squared_bounded(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [10.0, 50.0, 20.0, 80.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert 0.0 <= r_sq <= 1.0

    def test_empty_ys(self):
        slope, intercept, r_sq = _ols_forecast([], [])
        assert slope == 0.0
        assert r_sq == 0.0


# ── Fallback explanations ───────────────────────────────────────────────────


class TestFallbackExplain:
    def test_anomaly_context(self):
        result = _fallback_explain("Detected anomaly with z-score 5.2")
        assert "anomaly" in result.lower()

    def test_recommendation_context(self):
        result = _fallback_explain("Consider switch to cheaper model")
        assert "cheaper model" in result.lower()

    def test_generic_context(self):
        result = _fallback_explain("Some other analysis")
        assert "generated without ai" in result.lower()


# ── Claude API helper ────────────────────────────────────────────────────────


class TestClaudeGenerate:
    def test_returns_none_on_error(self):
        with patch("orchestrator.core.insights_engine.urllib_request.urlopen",
                    side_effect=Exception("Network error")):
            result = _claude_generate("system", "user", 100, "sk-test")
            assert result is None

    async def test_no_api_key_means_no_request(self):
        # Without a configured Anthropic key nothing is sent at all.
        from orchestrator.core.insights_engine import _ai_explain
        with patch("orchestrator.core.insights_engine.urllib_request.urlopen") as urlopen,                 patch("orchestrator.core.insights_engine._anthropic_api_key", return_value=""):
            out = await _ai_explain("system", "user")
        urlopen.assert_not_called()
        assert out
