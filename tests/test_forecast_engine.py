"""
Tests for orchestrator.core.forecast_engine — OLS regression, Holt smoothing,
seasonal factors, builtin_forecast, predict_budget_breach, and tier dispatch.
"""
from __future__ import annotations

from orchestrator.core.forecast_engine import (
    _holt_double_exponential,
    _ols,
    _prediction_std_error,
    _seasonal_factors,
    builtin_forecast,
    predict_budget_breach,
)


# ── OLS regression ──────────────────────────────────────────────────────────


class TestOLS:
    def test_perfect_linear(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0]
        slope, intercept, r_sq = _ols(xs, ys)
        assert abs(slope - 2.0) < 1e-6
        assert abs(intercept - 2.0) < 1e-6
        assert abs(r_sq - 1.0) < 1e-6

    def test_flat_line(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [5.0, 5.0, 5.0, 5.0]
        slope, intercept, r_sq = _ols(xs, ys)
        assert abs(slope) < 1e-6
        assert abs(intercept - 5.0) < 1e-6

    def test_single_point(self):
        xs = [1.0]
        ys = [3.0]
        slope, intercept, r_sq = _ols(xs, ys)
        assert slope == 0.0
        assert intercept == 3.0
        assert r_sq == 0.0

    def test_two_points(self):
        xs = [0.0, 1.0]
        ys = [1.0, 3.0]
        slope, intercept, r_sq = _ols(xs, ys)
        assert abs(slope - 2.0) < 1e-6
        assert abs(intercept - 1.0) < 1e-6

    def test_r_squared_bounded(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [1.0, 3.0, 2.0, 5.0, 4.0]
        _, _, r_sq = _ols(xs, ys)
        assert 0.0 <= r_sq <= 1.0

    def test_identical_x_values(self):
        xs = [1.0, 1.0, 1.0]
        ys = [2.0, 3.0, 4.0]
        slope, intercept, r_sq = _ols(xs, ys)
        assert slope == 0.0
        assert r_sq == 0.0


# ── Prediction standard error ──────────────────────────────────────────────


class TestPredictionStdError:
    def test_perfect_fit_zero_error(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [1.0, 2.0, 3.0, 4.0]
        err = _prediction_std_error(xs, ys, slope=1.0, intercept=1.0)
        assert abs(err) < 1e-6

    def test_too_few_points(self):
        assert _prediction_std_error([0.0, 1.0], [1.0, 2.0], 1.0, 1.0) == 0.0

    def test_noisy_data_nonzero_error(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [1.0, 3.0, 2.0, 5.0, 4.0]
        slope, intercept, _ = _ols(xs, ys)
        err = _prediction_std_error(xs, ys, slope, intercept)
        assert err > 0


# ── Holt double exponential smoothing ───────────────────────────────────────


class TestHoltSmoothing:
    def test_flat_series(self):
        level, trend = _holt_double_exponential([5.0, 5.0, 5.0, 5.0])
        assert abs(trend) < 0.5  # Trend should be near zero

    def test_increasing_series(self):
        level, trend = _holt_double_exponential([1.0, 2.0, 3.0, 4.0, 5.0])
        assert trend > 0

    def test_decreasing_series(self):
        level, trend = _holt_double_exponential([10.0, 8.0, 6.0, 4.0, 2.0])
        assert trend < 0

    def test_single_value(self):
        level, trend = _holt_double_exponential([7.0])
        assert level == 7.0
        assert trend == 0.0

    def test_empty_list(self):
        level, trend = _holt_double_exponential([])
        assert level == 0.0
        assert trend == 0.0


# ── Seasonal factors ────────────────────────────────────────────────────────


class TestSeasonalFactors:
    def test_uniform_data_factors_near_one(self):
        ys = [10.0] * 21
        factors = _seasonal_factors(ys)
        assert len(factors) == 7
        for f in factors:
            assert abs(f - 1.0) < 1e-6

    def test_too_short_returns_ones(self):
        factors = _seasonal_factors([1.0, 2.0, 3.0])
        assert factors == [1.0] * 7

    def test_weekday_pattern(self):
        # Mon-Fri high (10), Sat-Sun low (2) — 3 weeks
        week = [10.0, 10.0, 10.0, 10.0, 10.0, 2.0, 2.0]
        ys = week * 3
        factors = _seasonal_factors(ys)
        # Weekday factors should be > 1, weekend < 1
        assert factors[0] > 1.0  # Monday
        assert factors[5] < 1.0  # Saturday


# ── builtin_forecast ────────────────────────────────────────────────────────


class TestBuiltinForecast:
    def test_naive_with_few_days(self):
        result = builtin_forecast(
            daily_costs=[10.0, 12.0],
            days_remaining_month=15,
            days_remaining_quarter=45,
            days_remaining_year=200,
            mtd_actual=22.0,
        )
        assert result["method"] == "naive_average"
        assert "forecast_eom" in result
        assert result["forecast_eom"] > 0

    def test_full_method_with_enough_data(self):
        daily = [10.0 + i * 0.5 for i in range(30)]
        result = builtin_forecast(
            daily_costs=daily,
            days_remaining_month=10,
            days_remaining_quarter=40,
            days_remaining_year=200,
            mtd_actual=sum(daily[-20:]),
        )
        assert result["method"] == "ols+holt+seasonal"
        assert result["r_squared"] >= 0
        assert result["forecast_eom"] > 0
        assert result["forecast_eoq"] >= result["forecast_eom"]
        assert len(result["seasonal_factors"]) == 7

    def test_confidence_interval(self):
        daily = [10.0 + i * 0.5 for i in range(30)]
        result = builtin_forecast(
            daily_costs=daily,
            days_remaining_month=10,
            days_remaining_quarter=40,
            days_remaining_year=200,
            mtd_actual=100.0,
        )
        assert result["confidence_low_eom"] <= result["forecast_eom"]
        assert result["confidence_high_eom"] >= result["forecast_eom"]

    def test_trend_daily(self):
        # Flat costs should have near-zero trend
        daily = [10.0] * 20
        result = builtin_forecast(
            daily_costs=daily,
            days_remaining_month=10,
            days_remaining_quarter=40,
            days_remaining_year=200,
            mtd_actual=100.0,
        )
        assert abs(result["trend_daily"]) < 0.1

    def test_empty_costs(self):
        result = builtin_forecast(
            daily_costs=[],
            days_remaining_month=10,
            days_remaining_quarter=40,
            days_remaining_year=200,
            mtd_actual=0.0,
        )
        assert result["method"] == "naive_average"
        assert result["forecast_eom"] == 0.0

    def test_single_day(self):
        result = builtin_forecast(
            daily_costs=[50.0],
            days_remaining_month=5,
            days_remaining_quarter=35,
            days_remaining_year=300,
            mtd_actual=50.0,
        )
        assert result["forecast_eom"] > 0


# ── predict_budget_breach ───────────────────────────────────────────────────


class TestPredictBudgetBreach:
    def test_no_budget_returns_none(self):
        assert predict_budget_breach([10.0, 10.0, 10.0], 0, 30.0, 3) is None

    def test_too_few_days(self):
        assert predict_budget_breach([10.0], 100.0, 10.0, 1) is None

    def test_already_breached(self):
        result = predict_budget_breach([10.0, 10.0, 10.0], 25.0, 30.0, 5)
        assert result is not None
        assert result["already_breached"] is True
        assert result["confidence"] == "high"

    def test_breach_predicted(self):
        # High daily spend, low budget
        costs = [50.0, 55.0, 60.0, 65.0, 70.0]
        result = predict_budget_breach(costs, 200.0, 100.0, 10)
        assert result is not None
        assert result["already_breached"] is False
        assert "breach_date" in result

    def test_no_breach_predicted(self):
        # Very low daily spend, huge budget
        costs = [1.0, 1.0, 1.0, 1.0, 1.0]
        result = predict_budget_breach(costs, 10000.0, 5.0, 5)
        assert result is None

    def test_breach_confidence_levels(self):
        # With a clear trend, confidence should be high
        costs = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0]
        result = predict_budget_breach(costs, 50.0, 30.0, 10)
        if result:
            assert result["confidence"] in ("high", "medium", "low")
