"""Tests for orchestrator.core.insights_engine — settings, fallback, OLS."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from orchestrator.core.insights_engine import (
    _DEFAULTS,
    _fallback_explain,
    _ols_forecast,
    get_setting,
    get_setting_bool,
    get_setting_float,
    get_setting_int,
)


# ── Settings helpers ─────────────────────────────────────────────────────────

class TestGetSetting:
    @pytest.mark.asyncio
    async def test_returns_db_value(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "custom_value"
        mock_db.get.return_value = mock_row
        result = await get_setting(mock_db, "task.anomaly_scan.enabled")
        assert result == "custom_value"

    @pytest.mark.asyncio
    async def test_returns_default_when_no_row(self):
        mock_db = AsyncMock()
        mock_db.get.return_value = None
        result = await get_setting(mock_db, "task.anomaly_scan.enabled")
        assert result == "true"

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_key(self):
        mock_db = AsyncMock()
        mock_db.get.return_value = None
        result = await get_setting(mock_db, "nonexistent.key")
        assert result == ""


class TestGetSettingInt:
    @pytest.mark.asyncio
    async def test_returns_int(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "42"
        mock_db.get.return_value = mock_row
        result = await get_setting_int(mock_db, "key", default=0)
        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_default_on_invalid(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "not-a-number"
        mock_db.get.return_value = mock_row
        result = await get_setting_int(mock_db, "key", default=99)
        assert result == 99


class TestGetSettingFloat:
    @pytest.mark.asyncio
    async def test_returns_float(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "3.14"
        mock_db.get.return_value = mock_row
        result = await get_setting_float(mock_db, "key", default=0.0)
        assert result == pytest.approx(3.14)

    @pytest.mark.asyncio
    async def test_returns_default_on_invalid(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "bad"
        mock_db.get.return_value = mock_row
        result = await get_setting_float(mock_db, "key", default=1.5)
        assert result == 1.5


class TestGetSettingBool:
    @pytest.mark.asyncio
    async def test_true_values(self):
        mock_db = AsyncMock()
        for val in ("true", "1", "yes", "on"):
            mock_row = MagicMock()
            mock_row.value = val
            mock_db.get.return_value = mock_row
            result = await get_setting_bool(mock_db, "key")
            assert result is True, f"Failed for {val}"

    @pytest.mark.asyncio
    async def test_false_values(self):
        mock_db = AsyncMock()
        for val in ("false", "0", "no", "off"):
            mock_row = MagicMock()
            mock_row.value = val
            mock_db.get.return_value = mock_row
            result = await get_setting_bool(mock_db, "key")
            assert result is False, f"Failed for {val}"


# ── _fallback_explain ────────────────────────────────────────────────────────

class TestFallbackExplain:
    def test_anomaly_context(self):
        result = _fallback_explain("anomaly detected at high z-score")
        assert "anomaly" in result.lower() or "baseline" in result.lower()

    def test_recommend_context(self):
        result = _fallback_explain("recommend switching model")
        assert "model" in result.lower() or "switch" in result.lower()

    def test_generic_context(self):
        result = _fallback_explain("some other context")
        assert "AI" in result or "Claude" in result or "generated" in result.lower()


# ── OLS forecast ─────────────────────────────────────────────────────────────

class TestOlsForecast:
    def test_two_points(self):
        slope, intercept, r_sq = _ols_forecast([0.0, 1.0], [10.0, 20.0])
        assert slope == pytest.approx(10.0)
        assert intercept == pytest.approx(10.0)
        assert r_sq == pytest.approx(1.0)

    def test_single_point(self):
        slope, intercept, r_sq = _ols_forecast([0.0], [5.0])
        assert slope == 0.0
        assert intercept == 5.0
        assert r_sq == 0.0

    def test_empty(self):
        slope, intercept, r_sq = _ols_forecast([], [])
        assert slope == 0.0
        assert r_sq == 0.0

    def test_flat_line(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        ys = [5.0, 5.0, 5.0, 5.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert slope == pytest.approx(0.0)
        assert intercept == pytest.approx(5.0)

    def test_r_squared_clamped(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert 0.0 <= r_sq <= 1.0
        assert r_sq == pytest.approx(1.0)

    def test_noisy_data(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [1.1, 2.3, 2.8, 4.2, 4.9]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert slope > 0
        assert 0.0 <= r_sq <= 1.0

    def test_same_x_values(self):
        xs = [1.0, 1.0, 1.0]
        ys = [5.0, 6.0, 7.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert slope == 0.0  # ss_xx is ~0


# ── Defaults ─────────────────────────────────────────────────────────────────

class TestDefaults:
    def test_defaults_has_required_keys(self):
        assert "task.anomaly_scan.enabled" in _DEFAULTS
        assert "task.forecast.enabled" in _DEFAULTS
        assert "insights.anomaly.zscore_threshold" in _DEFAULTS

    def test_defaults_are_strings(self):
        for v in _DEFAULTS.values():
            assert isinstance(v, str)
