"""
Modus — PQC Assessment Engine Tests
==========================================
Tests for the PQC readiness assessment and HNDL risk simulation engine.

Covers pure functions directly, async DB functions with mocked sessions.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── Static Data Tests ─────────────────────────────────────────────────────────

class TestClassicalAlgorithmsCompleteness:
    """Verify the CLASSICAL_ALGORITHMS lookup table is well-formed."""

    def test_classical_algorithms_completeness(self) -> None:
        """All entries must have required fields: key_size, quantum_break_year, replacement, category."""
        required_fields = {"key_size", "quantum_break_year", "replacement", "category"}
        for algo_name, info in CLASSICAL_ALGORITHMS.items():
            for field in required_fields:
                assert field in info, f"{algo_name} missing field '{field}'"
            assert isinstance(info["key_size"], int), f"{algo_name} key_size must be int"
            assert isinstance(info["quantum_break_year"], int), f"{algo_name} quantum_break_year must be int"
            assert isinstance(info["replacement"], str), f"{algo_name} replacement must be str"
            assert info["category"] in ("asymmetric", "symmetric", "key_exchange"), (
                f"{algo_name} has invalid category: {info['category']}"
            )

    def test_classical_algorithms_count(self) -> None:
        """Verify we have the expected number of algorithms."""
        assert len(CLASSICAL_ALGORITHMS) == 11

    def test_pqc_and_hybrid_disjoint(self) -> None:
        """PQC and hybrid sets must not overlap with classical."""
        classical_set = set(CLASSICAL_ALGORITHMS.keys())
        assert classical_set.isdisjoint(PQC_ALGORITHMS)
        assert classical_set.isdisjoint(HYBRID_ALGORITHMS)
        assert PQC_ALGORITHMS.isdisjoint(HYBRID_ALGORITHMS)


# ── detect_classical_crypto Tests ─────────────────────────────────────────────

class TestDetectClassicalCrypto:
    """Tests for the detect_classical_crypto pure function."""

    def test_detect_classical_crypto_rsa(self) -> None:
        """Detects RSA-2048 in metadata."""
        metadata = {"algorithm": "RSA-2048", "purpose": "signing"}
        result = detect_classical_crypto(metadata)
        assert len(result) == 1
        assert result[0]["algorithm"] == "RSA-2048"
        assert result[0]["info"]["category"] == "asymmetric"
        assert result[0]["info"]["replacement"] == "ML-KEM-768"

    def test_detect_classical_crypto_multiple(self) -> None:
        """Detects multiple classical algorithms in metadata."""
        metadata = {
            "encryption": "AES-128",
            "signature": "ECDSA-P256",
            "key_exchange": "ECDH-P256",
        }
        result = detect_classical_crypto(metadata)
        algo_names = {r["algorithm"] for r in result}
        assert "AES-128" in algo_names
        assert "ECDSA-P256" in algo_names
        assert "ECDH-P256" in algo_names
        assert len(result) == 3

    def test_detect_classical_crypto_no_match(self) -> None:
        """Returns empty list when only PQC algorithms present."""
        metadata = {"algorithm": "ML-KEM-768", "signature": "ML-DSA-65"}
        result = detect_classical_crypto(metadata)
        assert result == []

    def test_detect_classical_crypto_empty_metadata(self) -> None:
        """Handles empty dict gracefully."""
        result = detect_classical_crypto({})
        assert result == []

    def test_detect_classical_crypto_non_string_values(self) -> None:
        """Ignores non-string values in metadata."""
        metadata = {"algorithm": 12345, "key_size": None, "name": "RSA-2048"}
        result = detect_classical_crypto(metadata)
        # Should detect RSA-2048 in the "name" field
        assert len(result) == 1
        assert result[0]["algorithm"] == "RSA-2048"

    def test_detect_classical_crypto_deduplicates(self) -> None:
        """Does not report the same algorithm twice."""
        metadata = {
            "primary_algo": "RSA-2048",
            "backup_algo": "RSA-2048",
        }
        result = detect_classical_crypto(metadata)
        assert len(result) == 1


# ── compute_readiness_score Tests ─────────────────────────────────────────────

class TestComputeReadinessScore:
    """Tests for the compute_readiness_score pure function."""

    def test_compute_readiness_all_pqc(self) -> None:
        """All PQC keys should yield score of 100."""
        score = compute_readiness_score(
            classical_count=0, hybrid_count=0, pqc_count=10, weakest_algorithm=None,
        )
        assert score == 100.0

    def test_compute_readiness_all_classical(self) -> None:
        """All classical keys should yield base score of 0, possibly with penalty."""
        score = compute_readiness_score(
            classical_count=10, hybrid_count=0, pqc_count=0, weakest_algorithm="3DES",
        )
        # Base is 0, penalty for 3DES (break year 2025, already past)
        assert score == 0.0  # Clamped to 0

    def test_compute_readiness_mixed(self) -> None:
        """Mixed crypto should return proportional score."""
        # 5 PQC (500) + 3 hybrid (150) + 2 classical (0) = 650 / 10 = 65
        score = compute_readiness_score(
            classical_count=2, hybrid_count=3, pqc_count=5, weakest_algorithm=None,
        )
        assert score == 65.0

    def test_compute_readiness_zero_total(self) -> None:
        """Zero total keys should return 100 (nothing to migrate)."""
        score = compute_readiness_score(
            classical_count=0, hybrid_count=0, pqc_count=0, weakest_algorithm=None,
        )
        assert score == 100.0

    def test_compute_readiness_with_penalty(self) -> None:
        """Weakest algorithm near break year should reduce score."""
        # All hybrid: base = 50. ECDSA-P256 break year 2029, ~3 years away -> penalty 20
        score_with = compute_readiness_score(
            classical_count=0, hybrid_count=10, pqc_count=0, weakest_algorithm="ECDSA-P256",
        )
        score_without = compute_readiness_score(
            classical_count=0, hybrid_count=10, pqc_count=0, weakest_algorithm=None,
        )
        assert score_with < score_without

    def test_compute_readiness_clamp_upper(self) -> None:
        """Score never exceeds 100."""
        score = compute_readiness_score(
            classical_count=0, hybrid_count=0, pqc_count=1000, weakest_algorithm=None,
        )
        assert score <= 100.0


# ── estimate_quantum_break_year Tests ─────────────────────────────────────────

class TestEstimateQuantumBreakYear:
    """Tests for estimate_quantum_break_year."""

    def test_estimate_quantum_break_year_known(self) -> None:
        """Returns exact catalogued year for known algorithms."""
        assert estimate_quantum_break_year("RSA-2048", 2048) == 2030
        assert estimate_quantum_break_year("3DES", 168) == 2025
        assert estimate_quantum_break_year("AES-256", 256) == 2060
        assert estimate_quantum_break_year("ECDSA-P256", 256) == 2029

    def test_estimate_quantum_break_year_unknown(self) -> None:
        """Returns a reasonable heuristic estimate for unknown algorithms."""
        current_year = datetime.now(timezone.utc).year
        year = estimate_quantum_break_year("UnknownAlgo", 256)
        # Should be a future year within reasonable range
        assert year >= current_year
        assert year <= current_year + 20

    def test_estimate_quantum_break_year_pqc(self) -> None:
        """PQC algorithms should return far-future year."""
        year = estimate_quantum_break_year("ML-KEM-768", 768)
        assert year >= 2100

    def test_estimate_quantum_break_year_small_key(self) -> None:
        """Small unknown keys should break sooner."""
        datetime.now(timezone.utc).year
        small = estimate_quantum_break_year("CustomCipher", 64)
        large = estimate_quantum_break_year("CustomCipher2", 4096)
        assert small < large


# ── assess_hndl_risk Tests ────────────────────────────────────────────────────

class TestAssessHNDLRisk:
    """Tests for the assess_hndl_risk pure function."""

    def test_assess_hndl_risk_safe(self) -> None:
        """AES-256 should be safe with standard sensitivity."""
        result = assess_hndl_risk(
            algorithm="AES-256", key_size=256,
            data_sensitivity="standard", horizon_years=10,
        )
        assert result["risk_level"] == "safe"
        assert result["enforcement_action"] == "allowed"
        assert result["estimated_quantum_break_year"] == 2060

    def test_assess_hndl_risk_critical(self) -> None:
        """3DES should be critical (break year 2025, already past)."""
        result = assess_hndl_risk(
            algorithm="3DES", key_size=168,
            data_sensitivity="standard", horizon_years=10,
        )
        assert result["risk_level"] == "critical"
        assert result["enforcement_action"] == "blocked"
        assert result["years_until_break"] <= 0

    def test_assess_hndl_risk_sensitivity_multiplier(self) -> None:
        """top_secret sensitivity should make risk worse."""
        standard = assess_hndl_risk(
            algorithm="RSA-4096", key_size=4096,
            data_sensitivity="standard", horizon_years=10,
        )
        top_secret = assess_hndl_risk(
            algorithm="RSA-4096", key_size=4096,
            data_sensitivity="top_secret", horizon_years=10,
        )
        # top_secret should have equal or worse risk level
        risk_order = {"safe": 0, "monitor": 1, "urgent": 2, "critical": 3}
        assert risk_order[top_secret["risk_level"]] >= risk_order[standard["risk_level"]]

    def test_assess_hndl_risk_all_fields_present(self) -> None:
        """Result dict must contain all required fields."""
        result = assess_hndl_risk(
            algorithm="RSA-2048", key_size=2048,
            data_sensitivity="standard", horizon_years=10,
        )
        required = {"risk_level", "estimated_quantum_break_year", "recommendation",
                     "enforcement_action", "years_until_break"}
        assert required.issubset(result.keys())

    def test_assess_hndl_risk_unknown_algorithm(self) -> None:
        """Unknown algorithms should still produce a valid assessment."""
        result = assess_hndl_risk(
            algorithm="FutureAlgo", key_size=512,
            data_sensitivity="standard", horizon_years=10,
        )
        assert result["risk_level"] in {"safe", "monitor", "urgent", "critical"}
        assert isinstance(result["estimated_quantum_break_year"], int)
        assert isinstance(result["years_until_break"], int)

    def test_assess_hndl_risk_unknown_sensitivity_default(self) -> None:
        """Unknown sensitivity defaults to multiplier of 1.0 (standard)."""
        standard = assess_hndl_risk(
            algorithm="RSA-2048", key_size=2048,
            data_sensitivity="standard", horizon_years=10,
        )
        unknown = assess_hndl_risk(
            algorithm="RSA-2048", key_size=2048,
            data_sensitivity="nonexistent", horizon_years=10,
        )
        assert standard["risk_level"] == unknown["risk_level"]


# ── Migration Ladder Tests ────────────────────────────────────────────────────

class TestMigrationLadder:
    """Tests for migration ladder stage ordering."""

    def test_migration_ladder_stages_ordered(self) -> None:
        """Stages must be sequential 1-6."""
        for i, stage in enumerate(MIGRATION_LADDER_STAGES):
            assert stage["stage"] == i + 1, f"Stage {i+1} has incorrect stage number"
            assert "name" in stage
            assert "description" in stage
            assert isinstance(stage["name"], str)
            assert isinstance(stage["description"], str)
            assert len(stage["name"]) > 0
            assert len(stage["description"]) > 0

    def test_migration_ladder_has_six_stages(self) -> None:
        """Must have exactly 6 stages."""
        assert len(MIGRATION_LADDER_STAGES) == 6

    def test_migration_ladder_stage_names_unique(self) -> None:
        """Stage names must be unique."""
        names = [s["name"] for s in MIGRATION_LADDER_STAGES]
        assert len(names) == len(set(names))


# ── Async DB Function Tests (with mocks) ─────────────────────────────────────

class TestAssessTeamPQCReadiness:
    """Tests for assess_team_pqc_readiness with mocked DB."""

    @pytest.mark.asyncio
    async def test_assess_team_returns_score(self) -> None:
        """Should return a valid assessment dict with score."""
        from orchestrator.core.pqc_assessment import assess_team_pqc_readiness

        mock_db = AsyncMock()

        # Mock: team apps query returns empty list
        mock_execute_result = MagicMock()
        mock_execute_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_execute_result)
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()

        result = await assess_team_pqc_readiness(mock_db, "team-123")

        assert "score" in result
        assert "team_id" in result
        assert result["team_id"] == "team-123"
        assert isinstance(result["score"], float)
        assert 0 <= result["score"] <= 100
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()


class TestRunPQCAssessmentCycle:
    """Tests for the background assessment cycle."""

    @pytest.mark.asyncio
    async def test_cycle_skips_when_disabled(self) -> None:
        """Should return immediately when pqc_assessment_enabled is False."""
        from orchestrator.core.pqc_assessment import run_pqc_assessment_cycle

        with patch("orchestrator.core.pqc_assessment.settings") as mock_settings:
            mock_settings.pqc_assessment_enabled = False
            await run_pqc_assessment_cycle()
            # Should complete without error and without touching DB
