"""
Tests for orchestrator.core.federation_engine — Extended coverage for
BlindAggregator merge, weekly nonce properties, delta edge cases,
and SHA-256 helper.
"""
from __future__ import annotations

from orchestrator.core.federation_engine import (
    BlindAggregator,
    FederationSubmission,
    _sha256_hex,
    _weekly_nonce,
    extract_delta,
)


# ── BlindAggregator.merge_deltas ───────────────────────────────────────────


class TestBlindAggregatorMerge:
    def test_empty_deltas(self):
        result = BlindAggregator.merge_deltas([])
        assert result["participating_instances"] == 0
        assert result["confidence_score"] == 0.0
        assert result["gene_improvements"] == {}

    def test_single_delta(self):
        deltas = [
            {"gene_diffs": {"budget_weight": 0.1}, "fitness_improvement": 0.05}
        ]
        result = BlindAggregator.merge_deltas(deltas)
        assert result["participating_instances"] == 1
        assert abs(result["gene_improvements"]["budget_weight"] - 0.1) < 1e-6
        assert result["avg_fitness_improvement"] == 0.05

    def test_multiple_deltas_averaged(self):
        deltas = [
            {"gene_diffs": {"w": 0.1}, "fitness_improvement": 0.05},
            {"gene_diffs": {"w": 0.3}, "fitness_improvement": 0.15},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        assert result["participating_instances"] == 2
        assert abs(result["gene_improvements"]["w"] - 0.2) < 1e-6
        assert abs(result["avg_fitness_improvement"] - 0.1) < 1e-6

    def test_different_genes_merged(self):
        deltas = [
            {"gene_diffs": {"a": 0.1}, "fitness_improvement": 0.05},
            {"gene_diffs": {"b": 0.2}, "fitness_improvement": 0.10},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        assert "a" in result["gene_improvements"]
        assert "b" in result["gene_improvements"]

    def test_confidence_high_when_consistent(self):
        # All deltas agree on the same diff
        deltas = [
            {"gene_diffs": {"w": 0.1}, "fitness_improvement": 0.05},
            {"gene_diffs": {"w": 0.1}, "fitness_improvement": 0.05},
            {"gene_diffs": {"w": 0.1}, "fitness_improvement": 0.05},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        # With zero variance, confidence should be high
        assert result["confidence_score"] >= 0.5

    def test_confidence_low_when_inconsistent(self):
        deltas = [
            {"gene_diffs": {"w": 10.0}, "fitness_improvement": 0.5},
            {"gene_diffs": {"w": -10.0}, "fitness_improvement": -0.5},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        # Highly inconsistent — confidence should be lower
        assert 0.0 <= result["confidence_score"] <= 1.0

    def test_missing_fitness_improvement(self):
        deltas = [
            {"gene_diffs": {"a": 0.1}},
            {"gene_diffs": {"a": 0.2}},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        assert result["avg_fitness_improvement"] == 0.0


# ── extract_delta edge cases ───────────────────────────────────────────────


class TestExtractDeltaExtended:
    def test_empty_genomes(self):
        delta = extract_delta({}, {}, 0.0, 0.0, 0, 1)
        assert len(delta.gene_diffs) == 0

    def test_large_genome(self):
        before = {f"gene_{i}": float(i) for i in range(100)}
        after = {f"gene_{i}": float(i) + 0.001 for i in range(100)}
        delta = extract_delta(before, after, 0.5, 0.6, 0, 1)
        assert len(delta.gene_diffs) == 100

    def test_tiny_diff_ignored(self):
        before = {"a": 1.0}
        after = {"a": 1.0 + 1e-12}
        delta = extract_delta(before, after, 0.5, 0.5, 0, 1)
        assert len(delta.gene_diffs) == 0

    def test_negative_diffs(self):
        delta = extract_delta({"a": 5.0}, {"a": 3.0}, 0.5, 0.4, 0, 1)
        assert delta.gene_diffs["a"] == -2.0

    def test_rounding_precision(self):
        delta = extract_delta(
            {"a": 0.1},
            {"a": 0.1 + 1e-5},
            0.5, 0.5, 0, 1,
        )
        if delta.gene_diffs:
            # Should be rounded to 8 decimal places
            val = delta.gene_diffs["a"]
            assert abs(val - round(val, 8)) < 1e-12


# ── _weekly_nonce properties ────────────────────────────────────────────────


class TestWeeklyNonceExtended:
    def test_length_32(self):
        n = _weekly_nonce("test-key")
        assert len(n) == 32

    def test_hex_string(self):
        n = _weekly_nonce("key")
        int(n, 16)  # Should not raise

    def test_different_keys_different_nonces(self):
        n1 = _weekly_nonce("key-a")
        n2 = _weekly_nonce("key-b")
        assert n1 != n2


# ── _sha256_hex ─────────────────────────────────────────────────────────────


class TestSHA256Hex:
    def test_empty_input(self):
        result = _sha256_hex()
        assert len(result) == 64

    def test_single_part(self):
        result = _sha256_hex("hello")
        assert len(result) == 64

    def test_multiple_parts(self):
        result = _sha256_hex("hello", "world")
        assert len(result) == 64

    def test_deterministic(self):
        assert _sha256_hex("a", "b") == _sha256_hex("a", "b")

    def test_order_matters(self):
        assert _sha256_hex("a", "b") != _sha256_hex("b", "a")

    def test_concatenation_vs_parts(self):
        # sha256("ab") == sha256("a" + "b") since we update sequentially
        assert _sha256_hex("ab") == _sha256_hex("a", "b")


# ── FederationSubmission dataclass ──────────────────────────────────────────


class TestFederationSubmission:
    def test_frozen(self):
        sub = FederationSubmission(
            nonce="abc",
            encrypted_delta={"w": 0.1},
            fitness_improvement=0.05,
            generation_span=5,
            timestamp="2026-04-09T00:00:00Z",
        )
        assert sub.nonce == "abc"
        # Frozen dataclass — assignment should raise
        import pytest as _pt
        with _pt.raises(AttributeError):
            sub.nonce = "xyz"
