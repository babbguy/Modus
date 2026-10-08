"""
Tests for orchestrator.core.insights_engine — Settings helpers,
fallback explanations, and OLS forecast function.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.insights_engine import (
    _DEFAULTS,
    _fallback_explain,
    _ols_forecast,
    get_setting,
    get_setting_bool,
    get_setting_float,
    get_setting_int,
)


# ── _ols_forecast ───────────────────────────────────────────────────────────


class TestOLSForecast:
    def test_perfect_linear(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [10.0, 20.0, 30.0, 40.0, 50.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert abs(slope - 10.0) < 1e-6
        assert abs(intercept - 10.0) < 1e-6
        assert abs(r_sq - 1.0) < 1e-6

    def test_single_point(self):
        slope, intercept, r_sq = _ols_forecast([1.0], [5.0])
        assert slope == 0.0
        assert intercept == 5.0
        assert r_sq == 0.0

    def test_flat_line(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [7.0, 7.0, 7.0, 7.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert abs(slope) < 1e-6
        assert abs(intercept - 7.0) < 1e-6

    def test_r_squared_bounded(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [1.0, 4.0, 2.0, 5.0]
        _, _, r_sq = _ols_forecast(xs, ys)
        assert 0.0 <= r_sq <= 1.0

    def test_identical_x(self):
        slope, intercept, r_sq = _ols_forecast([1.0, 1.0, 1.0], [2.0, 3.0, 4.0])
        assert slope == 0.0
        assert r_sq == 0.0

    def test_empty_returns_zero(self):
        slope, intercept, r_sq = _ols_forecast([], [])
        assert slope == 0.0
        assert intercept == 0.0
        assert r_sq == 0.0


# ── _fallback_explain ───────────────────────────────────────────────────────


class TestFallbackExplain:
    def test_anomaly_context(self):
        result = _fallback_explain("App has anomaly z-score=5.2")
        assert "anomaly" in result.lower()
        assert len(result) > 0

    def test_recommend_context(self):
        result = _fallback_explain("recommend switch to cheaper model")
        assert "switch" in result.lower() or "model" in result.lower()

    def test_generic_context(self):
        result = _fallback_explain("some other analysis context")
        assert "AI" in result or "generated" in result.lower()


# ── get_setting helpers ─────────────────────────────────────────────────────


class TestGetSettingHelpers:
    @pytest.mark.asyncio
    async def test_get_setting_default(self):
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)
        val = await get_setting(db, "task.anomaly_scan.enabled")
        assert val == "true"

    @pytest.mark.asyncio
    async def test_get_setting_from_db(self):
        row = MagicMock()
        row.value = "false"
        db = AsyncMock()
        db.get = AsyncMock(return_value=row)
        val = await get_setting(db, "task.anomaly_scan.enabled")
        assert val == "false"

    @pytest.mark.asyncio
    async def test_get_setting_int_valid(self):
        row = MagicMock()
        row.value = "42"
        db = AsyncMock()
        db.get = AsyncMock(return_value=row)
        val = await get_setting_int(db, "key", default=0)
        assert val == 42

    @pytest.mark.asyncio
    async def test_get_setting_int_invalid(self):
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)
        # Key not in _DEFAULTS, so get_setting returns ""
        val = await get_setting_int(db, "nonexistent.key", default=99)
        assert val == 99

    @pytest.mark.asyncio
    async def test_get_setting_float_valid(self):
        row = MagicMock()
        row.value = "3.14"
        db = AsyncMock()
        db.get = AsyncMock(return_value=row)
        val = await get_setting_float(db, "key", default=0.0)
        assert abs(val - 3.14) < 1e-6

    @pytest.mark.asyncio
    async def test_get_setting_float_invalid(self):
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)
        val = await get_setting_float(db, "nonexistent.key", default=1.5)
        assert val == 1.5

    @pytest.mark.asyncio
    async def test_get_setting_bool_true_values(self):
        for v in ("true", "1", "yes", "on"):
            row = MagicMock()
            row.value = v
            db = AsyncMock()
            db.get = AsyncMock(return_value=row)
            assert await get_setting_bool(db, "key") is True

    @pytest.mark.asyncio
    async def test_get_setting_bool_false_values(self):
        for v in ("false", "0", "no", "off"):
            row = MagicMock()
            row.value = v
            db = AsyncMock()
            db.get = AsyncMock(return_value=row)
            assert await get_setting_bool(db, "key") is False


# ── Defaults consistency ────────────────────────────────────────────────────


class TestDefaults:
    def test_all_task_defaults_parseable(self):
        for key, val in _DEFAULTS.items():
            if "interval_seconds" in key:
                assert int(val) > 0, f"{key}={val} should be a positive int"
            elif "enabled" in key:
                assert val in ("true", "false"), f"{key}={val} should be bool string"

    def test_zscore_threshold_is_float(self):
        v = float(_DEFAULTS["insights.anomaly.zscore_threshold"])
        assert v > 0

    def test_baseline_days_is_int(self):
        v = int(_DEFAULTS["insights.anomaly.baseline_days"])
        assert v > 0
