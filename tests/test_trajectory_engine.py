"""
Tests for orchestrator.core.trajectory_engine — linear extrapolation,
Monte-Carlo simulation, vigil check, and SessionFingerprint.
"""

from __future__ import annotations

import pytest

from orchestrator.core.trajectory_engine import (
    SessionFingerprint,
    linear_extrapolate,
    monte_carlo_simulate,
    vigil_check,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_fingerprint(**overrides) -> SessionFingerprint:
    defaults = {
        "fingerprint_hash": "abc123",
        "avg_calls_per_session": 20.0,
        "std_calls": 5.0,
        "avg_cost_per_call": 0.01,
        "std_cost": 0.002,
        "call_count_p95": 30.0,
        "branching_factor": 0.5,
        "sample_count": 50,
    }
    defaults.update(overrides)
    return SessionFingerprint(**defaults)


# ── Linear Extrapolation ────────────────────────────────────────────────────


class TestLinearExtrapolation:
    def test_linear_extrapolation_basic(self):
        result = linear_extrapolate(
            current_spend=0.50,
            current_calls=5,
            avg_cost_per_call=0.01,
            avg_calls_per_session=20.0,
        )
        # remaining = 20 - 5 = 15, projected = 0.50 + 15 * 0.01 = 0.65
        assert result.p50_total_cost == pytest.approx(0.65, abs=1e-6)
        assert result.p75_total_cost == pytest.approx(0.65, abs=1e-6)
        assert result.p95_total_cost == pytest.approx(0.65, abs=1e-6)
        assert result.expected_remaining_calls == 15.0
        assert result.breach_probability == 0.0
        assert result.high_risk_paths == 0

    def test_linear_extrapolation_no_remaining_calls(self):
        result = linear_extrapolate(
            current_spend=1.00,
            current_calls=25,
            avg_cost_per_call=0.01,
            avg_calls_per_session=20.0,
        )
        # current_calls >= avg, remaining floored at 0
        assert result.expected_remaining_calls == 0.0
        assert result.p50_total_cost == pytest.approx(1.00, abs=1e-6)


# ── Monte-Carlo Simulation ─────────────────────────────────────────────────


class TestMonteCarloSimulation:
    def test_monte_carlo_simulation_basic(self):
        fp = _make_fingerprint()
        result = monte_carlo_simulate(
            current_spend=0.10,
            current_calls=5,
            fingerprint=fp,
            num_simulations=200,
        )
        # Percentiles must be ordered: p50 <= p75 <= p95
        assert result.p50_total_cost <= result.p75_total_cost
        assert result.p75_total_cost <= result.p95_total_cost
        # All projections should be at least current_spend
        assert result.p50_total_cost >= 0.10

    def test_monte_carlo_with_budget(self):
        fp = _make_fingerprint(
            avg_calls_per_session=30.0,
            avg_cost_per_call=0.05,
            std_cost=0.02,
        )
        result = monte_carlo_simulate(
            current_spend=0.50,
            current_calls=5,
            fingerprint=fp,
            budget=0.60,  # very tight budget
            num_simulations=200,
        )
        # With 25 remaining calls at ~$0.05 each, total ~$1.75, budget breach likely
        assert result.breach_probability > 0.0

    def test_monte_carlo_no_variance(self):
        fp = _make_fingerprint(std_calls=0.0, std_cost=0.0)
        result = monte_carlo_simulate(
            current_spend=0.10,
            current_calls=5,
            fingerprint=fp,
            num_simulations=200,
        )
        # With zero variance all simulations produce the same value
        assert result.p50_total_cost == pytest.approx(result.p75_total_cost, abs=1e-6)
        assert result.p75_total_cost == pytest.approx(result.p95_total_cost, abs=1e-6)


# ── Vigil Check ─────────────────────────────────────────────────────────────


class TestVigilCheck:
    def test_vigil_check_safe(self):
        result = vigil_check(["search", "read", "write"])
        assert result["safe"] is True
        assert result["reason"] == "ok"
        assert 0.0 <= result["risk_score"] <= 0.8

    def test_vigil_check_depth_exceeded(self):
        seq = ["tool"] * 30  # default max_tool_depth is 25
        result = vigil_check(seq)
        assert result["safe"] is False
        assert "depth" in result["reason"].lower()

    def test_vigil_check_loop_detected(self):
        # Default max_loop_iterations is 5, so 6 consecutive identical calls triggers
        seq = ["search"] * 6
        result = vigil_check(seq)
        assert result["safe"] is False
        assert "loop" in result["reason"].lower()

    def test_vigil_check_blocked_pattern(self):
        result = vigil_check(
            ["safe_tool", "exec_shell"],
            config={"blocked_tool_patterns": [r"exec_.*"]},
        )
        assert result["safe"] is False
        assert "blocked" in result["reason"].lower()
        assert result["risk_score"] == 1.0

    def test_vigil_check_amplification(self):
        # max_amplification_per_step defaults to 10; generate 15 distinct tools
        # in a window to trigger amplification detection
        distinct_tools = [f"tool_{i}" for i in range(15)]
        result = vigil_check(
            distinct_tools,
            config={"max_amplification_per_step": 5},
        )
        assert result["safe"] is False
        assert "amplification" in result["reason"].lower()

    def test_vigil_check_custom_config(self):
        # Lower depth limit to 3
        result = vigil_check(
            ["a", "b", "c", "d"],
            config={"max_tool_depth": 3},
        )
        assert result["safe"] is False
        assert "depth" in result["reason"].lower()

        # Raise loop limit so 6 identical calls are fine
        result2 = vigil_check(
            ["x"] * 6,
            config={"max_loop_iterations": 10},
        )
        assert result2["safe"] is True
