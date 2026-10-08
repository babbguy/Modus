"""Tests for orchestrator.core.pqc_assessment — readiness, HNDL, detection."""

import pytest

from orchestrator.core.pqc_assessment import (
    CLASSICAL_ALGORITHMS,
    HYBRID_ALGORITHMS,
    MIGRATION_LADDER_STAGES,
    PQC_ALGORITHMS,
    assess_hndl_risk,
    compute_readiness_score,
    detect_classical_crypto,
    estimate_quantum_break_year,
)


# ── detect_classical_crypto ──────────────────────────────────────────────────

class TestDetectClassicalCrypto:
    def test_detects_rsa(self):
        metadata = {"algorithm": "RSA-2048", "key_size": 2048}
        result = detect_classical_crypto(metadata)
        assert len(result) >= 1
        algos = [r["algorithm"] for r in result]
        assert "RSA-2048" in algos

    def test_detects_ecdsa(self):
        metadata = {"signature": "ECDSA-P256 certificate"}
        result = detect_classical_crypto(metadata)
        algos = [r["algorithm"] for r in result]
        assert "ECDSA-P256" in algos

    def test_empty_metadata(self):
        assert detect_classical_crypto({}) == []
        assert detect_classical_crypto(None) == []

    def test_no_match(self):
        metadata = {"format": "json", "version": "1.0"}
        result = detect_classical_crypto(metadata)
        assert len(result) == 0

    def test_non_string_values_skipped(self):
        metadata = {"key_size": 2048, "count": 5}
        result = detect_classical_crypto(metadata)
        assert len(result) == 0

    def test_deduplication(self):
        metadata = {
            "algorithm": "RSA-2048",
            "encryption": "RSA-2048 encryption",
        }
        result = detect_classical_crypto(metadata)
        algos = [r["algorithm"] for r in result]
        assert algos.count("RSA-2048") == 1


# ── compute_readiness_score ──────────────────────────────────────────────────

class TestComputeReadinessScore:
    def test_no_assets(self):
        assert compute_readiness_score(0, 0, 0, None) == 100.0

    def test_all_pqc(self):
        score = compute_readiness_score(0, 0, 10, None)
        assert score == 100.0

    def test_all_classical(self):
        score = compute_readiness_score(10, 0, 0, "3DES")
        assert score < 50  # Heavy penalty for old algorithm

    def test_hybrid_mix(self):
        score = compute_readiness_score(5, 5, 0, None)
        assert score == pytest.approx(25.0)  # 5*50 / 10

    def test_penalty_near_break(self):
        score_no_penalty = compute_readiness_score(5, 0, 5, None)
        score_with_penalty = compute_readiness_score(5, 0, 5, "3DES")
        assert score_with_penalty < score_no_penalty

    def test_clamped_to_zero(self):
        score = compute_readiness_score(100, 0, 0, "3DES")
        assert score >= 0.0

    def test_clamped_to_hundred(self):
        score = compute_readiness_score(0, 0, 100, None)
        assert score <= 100.0


# ── estimate_quantum_break_year ──────────────────────────────────────────────

class TestEstimateQuantumBreakYear:
    def test_known_algorithm(self):
        year = estimate_quantum_break_year("RSA-2048", 2048)
        assert year == 2030

    def test_pqc_algorithm(self):
        year = estimate_quantum_break_year("ML-KEM-768", 768)
        assert year == 2100

    def test_hybrid_algorithm(self):
        year = estimate_quantum_break_year("ML-KEM-768+X25519", 256)
        assert year == 2100

    def test_unknown_small_key(self):
        year = estimate_quantum_break_year("CUSTOM-64", 64)
        from datetime import datetime, timezone
        current = datetime.now(timezone.utc).year
        assert year == current + 5

    def test_unknown_medium_key(self):
        year = estimate_quantum_break_year("CUSTOM-256", 256)
        from datetime import datetime, timezone
        current = datetime.now(timezone.utc).year
        assert year == current + 10


# ── assess_hndl_risk ─────────────────────────────────────────────────────────

class TestAssessHndlRisk:
    def test_safe(self):
        result = assess_hndl_risk("AES-256", 256, "public", 5)
        assert result["risk_level"] == "safe"

    def test_critical_old_algorithm(self):
        result = assess_hndl_risk("3DES", 168, "top_secret", 10)
        assert result["risk_level"] == "critical"

    def test_result_structure(self):
        result = assess_hndl_risk("RSA-2048", 2048, "confidential", 5)
        assert "risk_level" in result
        assert "estimated_quantum_break_year" in result
        assert "recommendation" in result
        assert "years_until_break" in result

    def test_top_secret_sensitivity(self):
        r_normal = assess_hndl_risk("RSA-4096", 4096, "public", 5)
        r_secret = assess_hndl_risk("RSA-4096", 4096, "top_secret", 5)
        # Top secret should have same or worse risk level
        risk_order = {"safe": 0, "monitor": 1, "urgent": 2, "critical": 3}
        assert risk_order.get(r_secret["risk_level"], 0) >= risk_order.get(r_normal["risk_level"], 0)


# ── Static data ──────────────────────────────────────────────────────────────

class TestStaticData:
    def test_classical_algorithms_populated(self):
        assert len(CLASSICAL_ALGORITHMS) > 5

    def test_classical_has_required_fields(self):
        for name, info in CLASSICAL_ALGORITHMS.items():
            assert "key_size" in info
            assert "quantum_break_year" in info
            assert "replacement" in info
            assert "category" in info

    def test_pqc_algorithms_populated(self):
        assert len(PQC_ALGORITHMS) > 0

    def test_hybrid_algorithms_populated(self):
        assert len(HYBRID_ALGORITHMS) > 0

    def test_migration_ladder_ordered(self):
        stages = [s["stage"] for s in MIGRATION_LADDER_STAGES]
        assert stages == sorted(stages)
        assert stages[0] == 1
