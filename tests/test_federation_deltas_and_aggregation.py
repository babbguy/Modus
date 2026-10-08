"""Tests for orchestrator.core.federation_engine — deltas, proofs, aggregation."""
import json

import pytest

from orchestrator.core.federation_engine import (
    BlindAggregator,
    ConstitutionDelta,
    FederationSubmission,
    SigmaProof,
    _sha256_hex,
    _weekly_nonce,
    encrypt_delta_payload,
    extract_delta,
    generate_sigma_proof,
)


# ── _weekly_nonce ────────────────────────────────────────────────────────────

class TestWeeklyNonce:
    def test_deterministic(self):
        n1 = _weekly_nonce("test-key")
        n2 = _weekly_nonce("test-key")
        assert n1 == n2

    def test_different_keys(self):
        n1 = _weekly_nonce("key1")
        n2 = _weekly_nonce("key2")
        assert n1 != n2

    def test_length(self):
        nonce = _weekly_nonce("any-key")
        assert len(nonce) == 32


# ── extract_delta ────────────────────────────────────────────────────────────

class TestExtractDelta:
    def test_basic(self):
        before = {"budget_cap": 100.0, "rate_limit": 50.0}
        after = {"budget_cap": 80.0, "rate_limit": 50.0}
        delta = extract_delta(before, after, 0.5, 0.7, 1, 5)
        assert "budget_cap" in delta.gene_diffs
        assert delta.gene_diffs["budget_cap"] == pytest.approx(-20.0)
        assert delta.fitness_before == 0.5
        assert delta.fitness_after == 0.7
        assert "rate_limit" not in delta.gene_diffs  # no change

    def test_new_gene(self):
        before = {"a": 1.0}
        after = {"a": 1.0, "b": 5.0}
        delta = extract_delta(before, after, 0.5, 0.8, 0, 1)
        assert "b" in delta.gene_diffs
        assert delta.gene_diffs["b"] == pytest.approx(5.0)

    def test_removed_gene(self):
        before = {"a": 1.0, "b": 5.0}
        after = {"a": 1.0}
        delta = extract_delta(before, after, 0.8, 0.6, 0, 1)
        assert "b" in delta.gene_diffs
        assert delta.gene_diffs["b"] == pytest.approx(-5.0)

    def test_empty(self):
        delta = extract_delta({}, {}, 0.0, 0.0, 0, 0)
        assert len(delta.gene_diffs) == 0


# ── _sha256_hex ──────────────────────────────────────────────────────────────

class TestSha256Hex:
    def test_deterministic(self):
        h1 = _sha256_hex("hello", "world")
        h2 = _sha256_hex("hello", "world")
        assert h1 == h2

    def test_different_inputs(self):
        h1 = _sha256_hex("a")
        h2 = _sha256_hex("b")
        assert h1 != h2

    def test_length(self):
        h = _sha256_hex("test")
        assert len(h) == 64


# ── generate_sigma_proof ─────────────────────────────────────────────────────

class TestGenerateSigmaProof:
    def test_proof_structure(self):
        delta = ConstitutionDelta(
            gene_diffs={"budget": -10.0},
            fitness_before=0.5,
            fitness_after=0.7,
            generation_from=1,
            generation_to=5,
        )
        proof = generate_sigma_proof(delta, 0.2, 3, 4)
        assert isinstance(proof, SigmaProof)
        assert len(proof.commitment) == 64
        assert len(proof.challenge) == 64
        assert len(proof.response) == 64
        assert "fitness_improvement_hash" in proof.public_inputs

    def test_proof_determinism_differs(self):
        """Each proof uses a random nonce, so should differ."""
        delta = ConstitutionDelta(
            gene_diffs={"x": 1.0},
            fitness_before=0.3,
            fitness_after=0.5,
            generation_from=0,
            generation_to=1,
        )
        p1 = generate_sigma_proof(delta, 0.2, 1, 1)
        p2 = generate_sigma_proof(delta, 0.2, 1, 1)
        # Commitments should differ due to random nonces
        assert p1.commitment != p2.commitment


# ── BlindAggregator ──────────────────────────────────────────────────────────

class TestBlindAggregator:
    def test_merge_empty(self):
        result = BlindAggregator.merge_deltas([])
        assert result["participating_instances"] == 0
        assert result["confidence_score"] == 0.0

    def test_merge_single(self):
        delta = {"gene_diffs": {"budget": -5.0}, "fitness_improvement": 0.1}
        result = BlindAggregator.merge_deltas([delta])
        assert result["participating_instances"] == 1
        assert result["gene_improvements"]["budget"] == pytest.approx(-5.0)

    def test_merge_multiple(self):
        deltas = [
            {"gene_diffs": {"budget": -5.0, "rate": 2.0}, "fitness_improvement": 0.1},
            {"gene_diffs": {"budget": -3.0, "rate": 4.0}, "fitness_improvement": 0.2},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        assert result["participating_instances"] == 2
        assert result["gene_improvements"]["budget"] == pytest.approx(-4.0)
        assert result["gene_improvements"]["rate"] == pytest.approx(3.0)
        assert result["avg_fitness_improvement"] == pytest.approx(0.15)

    def test_merge_consistency_score(self):
        """Consistent deltas should have high confidence."""
        deltas = [
            {"gene_diffs": {"x": 5.0}, "fitness_improvement": 0.1},
            {"gene_diffs": {"x": 5.0}, "fitness_improvement": 0.1},
            {"gene_diffs": {"x": 5.0}, "fitness_improvement": 0.1},
        ]
        result = BlindAggregator.merge_deltas(deltas)
        # All identical => stdev = 0 => cv = 0 => consistency = 1.0
        assert result["confidence_score"] >= 0.9


# ── encrypt_delta_payload ────────────────────────────────────────────────────

class TestEncryptDeltaPayload:
    def test_encrypt_returns_bytes(self):
        data = json.dumps({"budget": -5.0})
        key = b"test-encryption-key-for-federation"
        result = encrypt_delta_payload(data, key)
        assert isinstance(result, bytes)
        assert len(result) > len(data.encode())

    def test_different_inputs_different_output(self):
        key = b"key"
        r1 = encrypt_delta_payload("data1", key)
        r2 = encrypt_delta_payload("data2", key)
        assert r1 != r2


# ── ConstitutionDelta ────────────────────────────────────────────────────────

class TestConstitutionDelta:
    def test_frozen(self):
        delta = ConstitutionDelta(
            gene_diffs={"a": 1.0},
            fitness_before=0.5,
            fitness_after=0.7,
            generation_from=0,
            generation_to=1,
        )
        with pytest.raises(AttributeError):
            delta.fitness_before = 0.9


# ── FederationSubmission ─────────────────────────────────────────────────────

class TestFederationSubmission:
    def test_creation(self):
        sub = FederationSubmission(
            nonce="abc123",
            encrypted_delta={"x": 1.0},
            fitness_improvement=0.2,
            generation_span=5,
            timestamp="2026-01-01T00:00:00Z",
        )
        assert sub.nonce == "abc123"
        assert sub.fitness_improvement == 0.2
