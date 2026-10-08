"""
Insights engine deep coverage.

Targets orchestrator.core.insights_engine:
  - _claude_generate (127-129)
  - _ai_explain / _fallback_explain (135-157)
  - get_setting / get_setting_int / get_setting_float / get_setting_bool
  - _ols_forecast (355-386)
  - run_anomaly_scan (179-352) — specifically the early-exit paths
  - run_forecast_update (399-525) — early-exit paths
  - run_recommendation_refresh (587-717) — early-exit paths
  - run_prompt_efficiency_audit (752-878) — early-exit paths
  - run_executive_summary (898-933) — early-exit paths
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

from orchestrator.core.insights_engine import (
    _claude_generate,
    _ai_explain,
    _fallback_explain,
    get_setting,
    get_setting_int,
    get_setting_float,
    get_setting_bool,
    _ols_forecast,
    run_anomaly_scan,
    run_forecast_update,
    run_recommendation_refresh,
)
from orchestrator.core import insights_engine as ie_mod


# ── Settings helpers ─────────────────────────────────────────────────────────

class TestGetSetting:
    async def test_get_setting_from_db(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "custom_value"
        mock_db.get.return_value = mock_row
        result = await get_setting(mock_db, "some.key")
        assert result == "custom_value"

    async def test_get_setting_default(self):
        mock_db = AsyncMock()
        mock_db.get.return_value = None
        result = await get_setting(mock_db, "task.anomaly_scan.enabled")
        assert result == "true"

    async def test_get_setting_unknown_key(self):
        mock_db = AsyncMock()
        mock_db.get.return_value = None
        result = await get_setting(mock_db, "nonexistent.key")
        assert result == ""

    async def test_get_setting_int(self):
        mock_db = AsyncMock()
        mock_db.get.return_value = None
        result = await get_setting_int(mock_db, "task.anomaly_scan.interval_seconds")
        assert result == 300

    async def test_get_setting_int_invalid(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "not_a_number"
        mock_db.get.return_value = mock_row
        result = await get_setting_int(mock_db, "some.key", 42)
        assert result == 42

    async def test_get_setting_float(self):
        mock_db = AsyncMock()
        mock_db.get.return_value = None
        result = await get_setting_float(mock_db, "insights.anomaly.zscore_threshold")
        assert result == 3.0

    async def test_get_setting_float_invalid(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "bad"
        mock_db.get.return_value = mock_row
        result = await get_setting_float(mock_db, "x", 1.5)
        assert result == 1.5

    async def test_get_setting_bool_true(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "true"
        mock_db.get.return_value = mock_row
        result = await get_setting_bool(mock_db, "some.bool")
        assert result is True

    async def test_get_setting_bool_yes(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "yes"
        mock_db.get.return_value = mock_row
        result = await get_setting_bool(mock_db, "some.bool")
        assert result is True

    async def test_get_setting_bool_false(self):
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.value = "false"
        mock_db.get.return_value = mock_row
        result = await get_setting_bool(mock_db, "some.bool")
        assert result is False


# ── _claude_generate ─────────────────────────────────────────────────────────

class TestClaudeGenerate:
    def test_api_error(self):
        with patch("orchestrator.core.insights_engine.urllib_request") as mock_req:
            mock_req.Request.side_effect = Exception("Network error")
            result = _claude_generate("system prompt", "user prompt", 80, "sk-test")
        assert result is None

    def test_success_returns_text(self):
        with patch("orchestrator.core.insights_engine.urllib_request") as mock_req:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"content": [{"type": "text", "text": "Anomaly explanation"}]}'
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_req.urlopen.return_value = mock_response
            result = _claude_generate("system", "user", 80, "sk-test")
        assert result == "Anomaly explanation"


# ── _ai_explain / _fallback_explain ──────────────────────────────────────────

class TestFallbackExplain:
    def test_anomaly_context(self):
        result = _fallback_explain("Z-score anomaly detected on app-x")
        assert "anomaly" in result.lower()

    def test_recommendation_context(self):
        result = _fallback_explain("Recommend switch to cheaper model")
        assert "cheaper model" in result.lower() or "switch" in result.lower()

    def test_generic_context(self):
        result = _fallback_explain("Some other context")
        assert "without AI" in result


class TestAiExplain:
    async def test_fallback_on_failure(self):
        with patch.object(ie_mod, "_anthropic_api_key", return_value="sk-test"),                 patch.object(ie_mod, "_claude_generate", return_value=None):
            result = await _ai_explain("system", "anomaly detected")
        assert result is not None
        assert len(result) > 0

    async def test_success(self):
        with patch.object(ie_mod, "_anthropic_api_key", return_value="sk-test"),                 patch.object(ie_mod, "_claude_generate", return_value="AI explanation"):
            result = await _ai_explain("system", "user")
        assert result == "AI explanation"


# ── _ols_forecast ────────────────────────────────────────────────────────────

class TestOlsForecast:
    def test_simple_linear(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [10.0, 20.0, 30.0, 40.0, 50.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert abs(slope - 10.0) < 0.01
        assert abs(intercept - 10.0) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_flat_line(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [5.0, 5.0, 5.0, 5.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert abs(slope) < 0.01
        assert abs(intercept - 5.0) < 0.01

    def test_zero_variance_x(self):
        xs = [1.0, 1.0, 1.0]
        ys = [10.0, 20.0, 30.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert slope == 0.0  # degeneracy
        assert r_sq == 0.0

    def test_noisy_data(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [10.0, 12.0, 15.0, 14.0, 20.0]
        slope, intercept, r_sq = _ols_forecast(xs, ys)
        assert slope > 0  # generally upward
        assert 0 <= r_sq <= 1.0


# ── Background tasks early-exit ──────────────────────────────────────────────

class TestBackgroundTasksEarlyExit:
    async def test_anomaly_scan_no_session(self):
        with patch.object(ie_mod, "_session_factory", None):
            await run_anomaly_scan()  # should return immediately

    async def test_anomaly_scan_sqlite(self):
        mock_factory = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.is_sqlite = True
        with patch.object(ie_mod, "_session_factory", mock_factory), \
             patch("orchestrator.core.insights_engine.settings", mock_cfg, create=True):
            # Need to patch at module level too
            with patch("orchestrator.core.config.settings", mock_cfg):
                await run_anomaly_scan()

    async def test_forecast_no_session(self):
        with patch.object(ie_mod, "_session_factory", None):
            await run_forecast_update()

    async def test_forecast_sqlite(self):
        mock_factory = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.is_sqlite = True
        with patch.object(ie_mod, "_session_factory", mock_factory), \
             patch("orchestrator.core.config.settings", mock_cfg):
            await run_forecast_update()

    async def test_recommendations_no_session(self):
        with patch.object(ie_mod, "_session_factory", None):
            await run_recommendation_refresh()
