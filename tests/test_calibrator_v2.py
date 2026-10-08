"""
Tests for Routing Calibrator v2 and Drift Monitor v2.

Covers:
  - Feature vector utilities (centroid, variance, cosine distance, euclidean)
  - Calibration lifecycle (observe → calibrating → routing / excluded)
  - Drift detection (feature-based v2 + legacy v1 fallback)
  - SDK message sampling
  - Agreement rate computation
  - Edge cases and error handling
"""
from __future__ import annotations

import math
import struct
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.db.models import RoutingFingerprint, RoutingOutcome


# Uses engine / db_session fixtures from conftest.py


TEST_APP = "aaaaaaaa-0000-4000-8000-000000000001"  # routing rows reference apps.id


def _make_fingerprint(db, **overrides) -> RoutingFingerprint:
    defaults = dict(
        id=str(uuid.uuid4()),
        app_id=TEST_APP,
        fingerprint_hash=f"fp_{uuid.uuid4().hex[:16]}",
        system_prompt_hash="abc123",
        phase="observe",
        observe_call_count=200,
        observe_threshold=200,
        routing_confidence=0.0,
        calibration_sample_count=0,
        drift_score=0.0,
        confidence_decay_factor=1.0,
        max_misroute_rate=0.01,
        total_routed_calls=0,
        total_escalations=0,
        total_validator_failures=0,
        allow_routing=True,
    )
    defaults.update(overrides)
    fp = RoutingFingerprint(**defaults)
    db.add(fp)
    return fp


def _make_outcome(db, fingerprint_hash, **overrides) -> RoutingOutcome:
    defaults = dict(
        id=str(uuid.uuid4()),
        fingerprint_hash=fingerprint_hash,
        app_id=TEST_APP,
        routed_to="expensive",
        input_token_count=500,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    outcome = RoutingOutcome(**defaults)
    db.add(outcome)
    return outcome


# ══════════════════════════════════════════════════════════════════════════════
# Feature vector utilities
# ══════════════════════════════════════════════════════════════════════════════


class TestFeatureVectorUtilities:

    def test_compute_centroid_empty(self):
        from orchestrator.core.routing_calibrator import compute_centroid, FEATURE_KEYS
        centroid = compute_centroid([])
        assert all(centroid[k] == 0.0 for k in FEATURE_KEYS)

    def test_compute_centroid_single(self):
        from orchestrator.core.routing_calibrator import compute_centroid
        fv = {"system_prompt_length": 100, "user_prompt_length": 200}
        centroid = compute_centroid([fv])
        assert centroid["system_prompt_length"] == 100.0
        assert centroid["user_prompt_length"] == 200.0

    def test_compute_centroid_multiple(self):
        from orchestrator.core.routing_calibrator import compute_centroid
        fvs = [
            {"system_prompt_length": 100, "user_prompt_length": 200},
            {"system_prompt_length": 200, "user_prompt_length": 400},
        ]
        centroid = compute_centroid(fvs)
        assert centroid["system_prompt_length"] == 150.0
        assert centroid["user_prompt_length"] == 300.0

    def test_compute_variance(self):
        from orchestrator.core.routing_calibrator import compute_centroid, compute_variance
        fvs = [
            {"system_prompt_length": 100, "user_prompt_length": 200},
            {"system_prompt_length": 200, "user_prompt_length": 200},
        ]
        centroid = compute_centroid(fvs)
        variance = compute_variance(fvs, centroid)
        assert variance["system_prompt_length"] == 2500.0  # (50^2 + 50^2) / 2
        assert variance["user_prompt_length"] == 0.0  # no variance

    def test_compute_variance_single_sample(self):
        from orchestrator.core.routing_calibrator import compute_centroid, compute_variance
        fvs = [{"system_prompt_length": 100}]
        centroid = compute_centroid(fvs)
        variance = compute_variance(fvs, centroid)
        assert variance["system_prompt_length"] == 0.0

    def test_cosine_distance_identical(self):
        from orchestrator.core.routing_calibrator import cosine_distance
        a = {"system_prompt_length": 100, "user_prompt_length": 200}
        assert cosine_distance(a, a) == pytest.approx(0.0, abs=1e-10)

    def test_cosine_distance_orthogonal(self):
        from orchestrator.core.routing_calibrator import cosine_distance, FEATURE_KEYS
        a = {k: 0.0 for k in FEATURE_KEYS}
        b = {k: 0.0 for k in FEATURE_KEYS}
        a["system_prompt_length"] = 1.0
        b["user_prompt_length"] = 1.0
        dist = cosine_distance(a, b)
        assert dist == pytest.approx(1.0, abs=1e-10)

    def test_cosine_distance_zero_vectors(self):
        from orchestrator.core.routing_calibrator import cosine_distance, FEATURE_KEYS
        a = {k: 0.0 for k in FEATURE_KEYS}
        b = {k: 0.0 for k in FEATURE_KEYS}
        assert cosine_distance(a, b) == 0.0  # Both zero → no distance

    def test_euclidean_distance_normalized(self):
        from orchestrator.core.routing_calibrator import euclidean_distance_normalized, FEATURE_KEYS
        a = {k: 0.0 for k in FEATURE_KEYS}
        b = {k: 0.0 for k in FEATURE_KEYS}
        a["system_prompt_length"] = 100.0
        b["system_prompt_length"] = 200.0
        variance = {k: 0.0 for k in FEATURE_KEYS}
        variance["system_prompt_length"] = 10000.0  # std = 100
        dist = euclidean_distance_normalized(a, b, variance)
        # (200-100)/100 = 1.0, sqrt(1/12) ≈ 0.289
        expected = math.sqrt(1.0 / len(FEATURE_KEYS))
        assert dist == pytest.approx(expected, abs=0.01)


class TestFeatureExtraction:

    def test_extract_features_from_message(self):
        from orchestrator.core.routing_calibrator import _extract_features_from_message
        features = _extract_features_from_message("What is the weather today?")
        assert features["user_prompt_length"] == 26
        assert features["user_word_count"] == 5
        assert features["system_prompt_length"] == 0
        assert features["has_json_instruction"] == 0

    def test_extract_features_empty_message(self):
        from orchestrator.core.routing_calibrator import _extract_features_from_message
        features = _extract_features_from_message("")
        assert features["user_prompt_length"] == 0
        assert features["user_word_count"] == 0

    def test_features_from_token_count(self):
        from orchestrator.core.routing_calibrator import _features_from_token_count
        features = _features_from_token_count(500)
        assert features["user_prompt_tokens"] == 500
        assert features["total_tokens"] == 500
        assert features["user_prompt_length"] == 2000  # 500 * 4
        assert features["system_prompt_length"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Calibration lifecycle
# ══════════════════════════════════════════════════════════════════════════════


class TestCalibration:

    @pytest.mark.asyncio
    async def test_calibrate_not_enough_data(self, db_session):
        """Fingerprint with too few outcomes stays in observe."""
        fp = _make_fingerprint(db_session)
        await db_session.commit()

        # Only 5 outcomes (below min_samples=10)
        for i in range(5):
            _make_outcome(db_session, fp.fingerprint_hash, input_token_count=500)
        await db_session.commit()

        from orchestrator.core.routing_calibrator import _run_calibration
        await _run_calibration(db_session, fp)

        await db_session.refresh(fp)
        assert fp.phase == "observe"

    @pytest.mark.asyncio
    async def test_calibrate_promotes_to_routing(self, db_session):
        """Fingerprint with enough data and high agreement promotes to routing."""
        fp = _make_fingerprint(db_session, max_misroute_rate=0.10)
        await db_session.commit()

        # Create 20 VALIDATED routing outcomes (real agreement evidence, not a
        # cold-start guess): routed to cheap and the validator passed.
        for i in range(20):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=500,
                routed_to="cheap", validator_passed=True,
                sampled_user_message=f"What is the answer to question {i}?",
            )
        await db_session.commit()

        from orchestrator.core.routing_calibrator import _run_calibration
        await _run_calibration(db_session, fp)

        await db_session.refresh(fp)
        assert fp.phase == "routing"
        assert fp.routing_confidence == pytest.approx(1.0, abs=0.01)
        assert fp.calibration_centroid_json is not None
        assert fp.calibration_sample_count == 20
        assert fp.conformal_threshold is not None

    @pytest.mark.asyncio
    async def test_calibrate_with_token_count_fallback(self, db_session):
        """Calibration works with token count fallback when no sampled messages."""
        fp = _make_fingerprint(db_session, max_misroute_rate=0.10)
        await db_session.commit()

        for i in range(20):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=400 + (i * 10),
                routed_to="cheap", validator_passed=True,
            )
        await db_session.commit()

        from orchestrator.core.routing_calibrator import _run_calibration
        await _run_calibration(db_session, fp)

        await db_session.refresh(fp)
        assert fp.phase == "routing"
        assert fp.calibration_centroid_json is not None
        # Legacy binary centroid should also be set
        assert fp.calibration_centroid is not None

    @pytest.mark.asyncio
    async def test_calibrate_excludes_low_agreement(self, db_session):
        """Fingerprint with low agreement rate gets excluded."""
        fp = _make_fingerprint(db_session, max_misroute_rate=0.01)
        await db_session.commit()

        # Create outcomes with many escalations (low agreement)
        for i in range(15):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=500,
                routed_to="escalated",
                validator_passed=False,
            )
        for i in range(5):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=500,
                routed_to="cheap",
                validator_passed=True,
            )
        await db_session.commit()

        from orchestrator.core.routing_calibrator import _run_calibration
        await _run_calibration(db_session, fp)

        await db_session.refresh(fp)
        assert fp.phase == "excluded"

    @pytest.mark.asyncio
    async def test_calibrate_fingerprints_selects_eligible(self, db_session):
        """Only fingerprints with enough call count are selected."""
        # Eligible (200 calls)
        fp_ready = _make_fingerprint(
            db_session, observe_call_count=200,
            fingerprint_hash="fp_ready_00000000",
            max_misroute_rate=0.10,
        )
        # Not eligible (only 50 calls)
        fp_not_ready = _make_fingerprint(
            db_session, observe_call_count=50,
            fingerprint_hash="fp_notready_0000",
        )
        await db_session.commit()

        # Add outcomes for the ready one
        for i in range(20):
            _make_outcome(
                db_session, fp_ready.fingerprint_hash,
                input_token_count=500,
            )
        await db_session.commit()

        from orchestrator.core.routing_calibrator import calibrate_fingerprints

        with patch("orchestrator.core.routing_calibrator.get_session_ctx") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            await calibrate_fingerprints()

        await db_session.refresh(fp_ready)
        await db_session.refresh(fp_not_ready)
        # fp_ready should have been processed (promoted or stayed)
        assert fp_ready.phase in ("routing", "observe")  # promoted or not enough data
        assert fp_not_ready.phase == "observe"  # untouched


class TestAgreementRate:

    @pytest.mark.asyncio
    async def test_agreement_rate_with_data(self, db_session):
        """Agreement rate computed from validator pass rate."""
        fp = _make_fingerprint(db_session)
        await db_session.commit()

        # 8 passed, 2 failed
        for i in range(8):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                routed_to="cheap", validator_passed=True,
            )
        for i in range(2):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                routed_to="escalated", validator_passed=False,
            )
        await db_session.commit()

        from orchestrator.core.routing_calibrator import _compute_agreement_rate
        rate = await _compute_agreement_rate(db_session, fp)
        assert rate == pytest.approx(0.8, abs=0.01)

    @pytest.mark.asyncio
    async def test_agreement_rate_cold_start_returns_none(self, db_session):
        """Cold-start (no validated outcomes) signals None so the caller holds
        the fingerprint in observe rather than routing on an unproven guess.
        Previously this returned an optimistic 0.95 that triggered promotion."""
        fp = _make_fingerprint(db_session)
        await db_session.commit()

        from orchestrator.core.routing_calibrator import _compute_agreement_rate
        rate = await _compute_agreement_rate(db_session, fp)
        assert rate is None

    @pytest.mark.asyncio
    async def test_cold_start_fingerprint_is_not_promoted(self, db_session):
        """A fingerprint with no validated outcomes stays in observe, not routing."""
        fp = _make_fingerprint(db_session, max_misroute_rate=0.10)
        await db_session.commit()
        # 20 outcomes but all routed to the expensive model (not validated
        # cheap-routing evidence) — must NOT promote.
        for i in range(20):
            _make_outcome(db_session, fp.fingerprint_hash,
                          input_token_count=500, routed_to="expensive")
        await db_session.commit()

        from orchestrator.core.routing_calibrator import _run_calibration
        await _run_calibration(db_session, fp)
        await db_session.refresh(fp)
        assert fp.phase == "observe"
        assert fp.phase != "routing"


class TestOutcomeEvaluation:

    @pytest.mark.asyncio
    async def test_demotes_high_misroute_rate(self, db_session):
        """Fingerprint demoted to observe when misroute rate exceeds budget."""
        fp = _make_fingerprint(
            db_session, phase="routing",
            routing_confidence=0.95, max_misroute_rate=0.01,
        )
        await db_session.commit()

        # 5 escalations out of 25 total = 20% misroute rate (>> 1.5% budget)
        for i in range(20):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                routed_to="cheap",
            )
        for i in range(5):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                routed_to="escalated",
            )
        await db_session.commit()

        from orchestrator.core.routing_calibrator import evaluate_routing_outcomes

        with patch("orchestrator.core.routing_calibrator.get_session_ctx") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            await evaluate_routing_outcomes()

        await db_session.refresh(fp)
        assert fp.phase == "observe"
        assert fp.routing_confidence == 0.0

    @pytest.mark.asyncio
    async def test_keeps_routing_when_within_budget(self, db_session):
        """Fingerprint stays in routing when misroute rate is within budget."""
        fp = _make_fingerprint(
            db_session, phase="routing",
            routing_confidence=0.95, max_misroute_rate=0.05,
        )
        await db_session.commit()

        # 1 escalation out of 50 = 2% (within 7.5% = 5% * 1.5)
        for i in range(49):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                routed_to="cheap",
            )
        _make_outcome(
            db_session, fp.fingerprint_hash,
            routed_to="escalated",
        )
        await db_session.commit()

        from orchestrator.core.routing_calibrator import evaluate_routing_outcomes

        with patch("orchestrator.core.routing_calibrator.get_session_ctx") as mock_ctx:
            mock_ctx.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            await evaluate_routing_outcomes()

        await db_session.refresh(fp)
        assert fp.phase == "routing"


# ══════════════════════════════════════════════════════════════════════════════
# Drift detection
# ══════════════════════════════════════════════════════════════════════════════


class TestDriftDetection:

    @pytest.mark.asyncio
    async def test_drift_v2_no_drift(self, db_session):
        """No drift when rolling centroid matches calibration centroid."""
        from orchestrator.core.routing_calibrator import compute_centroid

        # Build a centroid from consistent data
        fvs = [{"system_prompt_length": 0, "user_prompt_length": 200,
                 "system_prompt_tokens": 1, "user_prompt_tokens": 50,
                 "total_tokens": 50, "has_json_instruction": 0,
                 "has_code_instruction": 0, "has_classification": 0,
                 "has_extraction": 0, "prompt_ratio": 200.0,
                 "system_word_count": 0, "user_word_count": 30}
               for _ in range(50)]
        cal_centroid = compute_centroid(fvs)

        fp = _make_fingerprint(
            db_session, phase="routing",
            routing_confidence=0.95,
            cheap_model_agreement_rate=0.95,
            calibration_centroid_json=cal_centroid,
            drift_threshold=2.5,
        )
        await db_session.commit()

        # Create outcomes matching calibration distribution
        for i in range(60):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=200,
                sampled_user_message="What is the weather today in the city?",
            )
        await db_session.commit()

        from orchestrator.core.drift_monitor import _check_fingerprint_drift
        await _check_fingerprint_drift(db_session, fp)

        await db_session.refresh(fp)
        assert fp.phase == "routing"

    @pytest.mark.asyncio
    async def test_drift_v1_fallback(self, db_session):
        """v1 fallback when no JSON centroid available."""
        fp = _make_fingerprint(
            db_session, phase="routing",
            routing_confidence=0.95,
            cheap_model_agreement_rate=0.95,
            calibration_centroid=struct.pack("d", 500.0),
            calibration_centroid_variance=50.0,
            drift_threshold=2.5,
        )
        await db_session.commit()

        # Create outcomes with similar token counts (no drift)
        for i in range(60):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=490 + (i % 20),
            )
        await db_session.commit()

        from orchestrator.core.drift_monitor import _check_fingerprint_drift
        await _check_fingerprint_drift(db_session, fp)

        await db_session.refresh(fp)
        assert fp.phase == "routing"

    @pytest.mark.asyncio
    async def test_drift_v1_detects_shift(self, db_session):
        """v1 detects drift when token distribution shifts significantly."""
        fp = _make_fingerprint(
            db_session, phase="routing",
            routing_confidence=0.95,
            cheap_model_agreement_rate=0.95,
            calibration_centroid=struct.pack("d", 500.0),
            calibration_centroid_variance=50.0,
            drift_threshold=2.5,
        )
        await db_session.commit()

        # Token counts shifted way up (drift!)
        for i in range(60):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=5000,
            )
        await db_session.commit()

        from orchestrator.core.drift_monitor import _check_fingerprint_drift
        await _check_fingerprint_drift(db_session, fp)
        await db_session.commit()

        await db_session.refresh(fp)
        assert fp.phase == "drift_flagged"
        assert fp.confidence_decay_factor < 1.0

    @pytest.mark.asyncio
    async def test_drift_recovery(self, db_session):
        """Confidence recovers when drift resolves."""
        fp = _make_fingerprint(
            db_session, phase="drift_flagged",
            routing_confidence=0.8,
            cheap_model_agreement_rate=0.95,
            confidence_decay_factor=0.80,
            calibration_centroid=struct.pack("d", 500.0),
            calibration_centroid_variance=50.0,
            drift_threshold=2.5,
        )
        await db_session.commit()

        # Token counts back to normal
        for i in range(60):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=500,
            )
        await db_session.commit()

        from orchestrator.core.drift_monitor import _check_fingerprint_drift
        await _check_fingerprint_drift(db_session, fp)
        await db_session.commit()

        await db_session.refresh(fp)
        # Decay factor should have recovered: 0.80 * 1.1 = 0.88
        assert fp.confidence_decay_factor > 0.80

    @pytest.mark.asyncio
    async def test_drift_not_enough_data(self, db_session):
        """Drift check is skipped with insufficient data."""
        fp = _make_fingerprint(
            db_session, phase="routing",
            routing_confidence=0.95,
            calibration_centroid=struct.pack("d", 500.0),
        )
        await db_session.commit()

        # Only 10 outcomes (below 50 threshold)
        for i in range(10):
            _make_outcome(
                db_session, fp.fingerprint_hash,
                input_token_count=500,
            )
        await db_session.commit()

        from orchestrator.core.drift_monitor import _check_fingerprint_drift
        await _check_fingerprint_drift(db_session, fp)

        await db_session.refresh(fp)
        assert fp.phase == "routing"  # Unchanged


class TestDriftHelpers:

    def test_apply_drift_decay_flags(self):
        from orchestrator.core.drift_monitor import _apply_drift_decay
        fp = MagicMock()
        fp.confidence_decay_factor = 1.0
        fp.cheap_model_agreement_rate = 0.95
        fp.routing_confidence = 0.95
        fp.fingerprint_hash = "test12345678"

        _apply_drift_decay(fp, 3.0, 2.5, 0.85)

        assert fp.phase == "drift_flagged"
        assert fp.confidence_decay_factor == pytest.approx(0.85)
        assert fp.routing_confidence == pytest.approx(0.95 * 0.85)

    def test_apply_drift_decay_re_observe(self):
        from orchestrator.core.drift_monitor import _apply_drift_decay
        fp = MagicMock()
        fp.confidence_decay_factor = 0.3  # Already heavily decayed
        fp.cheap_model_agreement_rate = 0.95
        fp.fingerprint_hash = "test12345678"

        _apply_drift_decay(fp, 3.0, 2.5, 0.85)

        # 0.3 * 0.85 = 0.255 → confidence = 0.95 * 0.255 = 0.242 < 0.5
        assert fp.phase == "observe"
        assert fp.observe_call_count == 0
        assert fp.routing_confidence == 0.0
        assert fp.confidence_decay_factor == 1.0

    def test_apply_drift_recovery_restores(self):
        from orchestrator.core.drift_monitor import _apply_drift_recovery
        fp = MagicMock()
        fp.confidence_decay_factor = 0.91
        fp.cheap_model_agreement_rate = 0.95
        fp.phase = "drift_flagged"

        _apply_drift_recovery(fp)

        # 0.91 * 1.1 = 1.001 → capped to 1.0
        assert fp.confidence_decay_factor == 1.0
        assert fp.phase == "routing"

    def test_apply_drift_recovery_noop_when_full(self):
        from orchestrator.core.drift_monitor import _apply_drift_recovery
        fp = MagicMock()
        fp.confidence_decay_factor = 1.0
        fp.phase = "routing"

        _apply_drift_recovery(fp)
        # No change expected
        assert fp.phase == "routing"


# ══════════════════════════════════════════════════════════════════════════════
# SDK message sampling
# ══════════════════════════════════════════════════════════════════════════════


class TestMessageSampling:

    def test_should_sample_message_probabilistic(self):
        from modus.routing_interceptor import should_sample_message
        # Run 1000 times, expect ~10% true
        results = [should_sample_message() for _ in range(1000)]
        rate = sum(results) / len(results)
        assert 0.03 < rate < 0.20  # Wide margin for randomness

    def test_extract_last_user_message_anthropic(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {
            "messages": [
                {"role": "user", "content": "Hello world"},
                {"role": "assistant", "content": "Hi there"},
                {"role": "user", "content": "What is the meaning of life?"},
            ]
        }
        msg = extract_last_user_message("anthropic", kwargs)
        assert msg == "What is the meaning of life?"

    def test_extract_last_user_message_anthropic_blocks(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Part one"},
                    {"type": "text", "text": "Part two"},
                ]},
            ]
        }
        msg = extract_last_user_message("anthropic", kwargs)
        assert msg == "Part one Part two"

    def test_extract_last_user_message_openai(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello from OpenAI"},
            ]
        }
        msg = extract_last_user_message("openai", kwargs)
        assert msg == "Hello from OpenAI"

    def test_extract_last_user_message_groq(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {"messages": [{"role": "user", "content": "Groq query"}]}
        msg = extract_last_user_message("groq", kwargs)
        assert msg == "Groq query"

    def test_extract_last_user_message_mistral(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {"messages": [{"role": "user", "content": "Mistral query"}]}
        msg = extract_last_user_message("mistral", kwargs)
        assert msg == "Mistral query"

    def test_extract_last_user_message_google(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {
            "contents": [
                {"role": "user", "parts": [{"text": "Google query"}]},
            ]
        }
        msg = extract_last_user_message("google", kwargs)
        assert msg == "Google query"

    def test_extract_last_user_message_google_string(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {"contents": "Simple string query"}
        msg = extract_last_user_message("google", kwargs)
        assert msg == "Simple string query"

    def test_extract_last_user_message_cohere(self):
        from modus.routing_interceptor import extract_last_user_message
        kwargs = {"message": "Cohere query"}
        msg = extract_last_user_message("cohere", kwargs)
        assert msg == "Cohere query"

    def test_extract_last_user_message_unknown_provider(self):
        from modus.routing_interceptor import extract_last_user_message
        msg = extract_last_user_message("unknown_provider", {})
        assert msg is None

    def test_extract_last_user_message_empty_messages(self):
        from modus.routing_interceptor import extract_last_user_message
        msg = extract_last_user_message("anthropic", {"messages": []})
        assert msg is None

    def test_extract_last_user_message_truncation(self):
        from modus.routing_interceptor import extract_last_user_message, _MAX_SAMPLED_LENGTH
        long_msg = "x" * 1000
        kwargs = {"messages": [{"role": "user", "content": long_msg}]}
        msg = extract_last_user_message("anthropic", kwargs)
        assert len(msg) == _MAX_SAMPLED_LENGTH

    def test_extract_last_user_message_error_handling(self):
        from modus.routing_interceptor import extract_last_user_message
        # Pass something that will cause an error in extraction
        msg = extract_last_user_message("anthropic", {"messages": "not_a_list"})
        assert msg is None


# ══════════════════════════════════════════════════════════════════════════════
# Task entry points
# ══════════════════════════════════════════════════════════════════════════════


class TestTaskEntryPoints:

    @pytest.mark.asyncio
    async def test_run_calibrator_cycle(self):
        from orchestrator.core.routing_calibrator import run_calibrator_cycle
        with patch("orchestrator.core.routing_calibrator.calibrate_fingerprints", new_callable=AsyncMock) as mock_cal, \
             patch("orchestrator.core.routing_calibrator.evaluate_routing_outcomes", new_callable=AsyncMock) as mock_eval:
            await run_calibrator_cycle()
            mock_cal.assert_called_once()
            mock_eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_drift_monitor_cycle(self):
        from orchestrator.core.drift_monitor import run_drift_monitor_cycle
        with patch("orchestrator.core.drift_monitor.check_drift", new_callable=AsyncMock) as mock_drift:
            await run_drift_monitor_cycle()
            mock_drift.assert_called_once()
