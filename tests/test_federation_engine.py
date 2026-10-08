"""
Tests for orchestrator.core.federation_engine —
Delta extraction, blind aggregation, weekly nonce, consent checkpoint.
"""
from __future__ import annotations


from orchestrator.core.federation_engine import (
    ConstitutionDelta,
    extract_delta,
    _weekly_nonce,
    BlindAggregator,
)


class TestExtractDelta:
    """Constitution delta extraction."""

    def test_extract_basic_delta(self):
        before = {"budget_weight": 0.5, "rate_weight": 0.3}
        after = {"budget_weight": 0.6, "rate_weight": 0.3}
        delta = extract_delta(before, after, 0.8, 0.85, 1, 2)
        assert isinstance(delta, ConstitutionDelta)
        assert "budget_weight" in delta.gene_diffs
        assert abs(delta.gene_diffs["budget_weight"] - 0.1) < 1e-6
        assert "rate_weight" not in delta.gene_diffs  # unchanged

    def test_extract_new_gene(self):
        before = {"a": 1.0}
        after = {"a": 1.0, "b": 0.5}
        delta = extract_delta(before, after, 0.5, 0.6, 0, 1)
        assert "b" in delta.gene_diffs
        assert delta.gene_diffs["b"] == 0.5

    def test_extract_removed_gene(self):
        before = {"a": 1.0, "b": 0.5}
        after = {"a": 1.0}
        delta = extract_delta(before, after, 0.5, 0.6, 0, 1)
        assert "b" in delta.gene_diffs
        assert delta.gene_diffs["b"] == -0.5

    def test_extract_no_change(self):
        genome = {"a": 1.0, "b": 2.0}
        delta = extract_delta(genome, genome, 0.5, 0.5, 0, 1)
        assert len(delta.gene_diffs) == 0

    def test_fitness_values_preserved(self):
        delta = extract_delta({"a": 1.0}, {"a": 2.0}, 0.3, 0.9, 5, 10)
        assert delta.fitness_before == 0.3
        assert delta.fitness_after == 0.9
        assert delta.generation_from == 5
        assert delta.generation_to == 10


class TestWeeklyNonce:
    """Weekly-rotating anonymous nonce."""

    def test_nonce_deterministic_same_key(self):
        n1 = _weekly_nonce("key-1")
        n2 = _weekly_nonce("key-1")
        assert n1 == n2

    def test_nonce_different_keys(self):
        n1 = _weekly_nonce("key-1")
        n2 = _weekly_nonce("key-2")
        assert n1 != n2

    def test_nonce_length(self):
        n = _weekly_nonce("test-key")
        assert len(n) == 32  # 32 hex chars


class TestBlindAggregator:
    """Blind delta aggregation."""

    def test_merge_empty(self):
        result = BlindAggregator.merge_deltas([])
        assert result["participating_instances"] == 0
        assert result["gene_improvements"] == {}

    def test_merge_single(self):
        deltas = [{"gene_diffs": {"a": 0.1}, "fitness_improvement": 0.05}]
        result = BlindAggregator.merge_deltas(deltas)
        assert result["participating_instances"] == 1
        assert abs(result["gene_improvements"]["a"] - 0.1) < 1e-6

    def test_merge_multiple_averages(self):
        deltas = [
            {"gene_diffs": {"a": 0.1, "b": 0.2}, "fitness_improvement": 0.05},
            {"gene_diffs": {"a": 0.3, "b": 0.2}, "fitness_improvement": 0.10},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        assert result["participating_instances"] == 2
        assert abs(result["gene_improvements"]["a"] - 0.2) < 1e-6  # avg of 0.1 and 0.3
        assert abs(result["gene_improvements"]["b"] - 0.2) < 1e-6

    def test_merge_confidence_score_bounded(self):
        deltas = [
            {"gene_diffs": {"a": 0.1}, "fitness_improvement": 0.05},
            {"gene_diffs": {"a": 0.1}, "fitness_improvement": 0.05},
            {"gene_diffs": {"a": 0.1}, "fitness_improvement": 0.05},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        assert 0.0 <= result["confidence_score"] <= 1.0
