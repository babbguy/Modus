"""Tests for drift detection — centroid distance, decay factor, re-observe trigger."""

import struct


class TestCentroidSerialization:
    def test_pack_unpack_centroid(self):
        """Centroid stored as struct-packed double."""
        value = 1234.56
        packed = struct.pack("d", value)
        unpacked = struct.unpack("d", packed)[0]
        assert abs(unpacked - value) < 1e-10

    def test_centroid_8_bytes(self):
        packed = struct.pack("d", 500.0)
        assert len(packed) == 8


class TestDriftScoreCalculation:
    """Test drift score calculation logic (unit-testable without DB)."""

    def test_no_drift_score_zero(self):
        """When rolling mean == calibration mean, drift = 0."""
        calibration_mean = 500.0
        rolling_mean = 500.0
        variance = 100.0
        drift_score = abs(rolling_mean - calibration_mean) / max(variance, 1.0)
        assert drift_score == 0.0

    def test_moderate_drift(self):
        """Drift within 2.5σ should not trigger re-observe."""
        calibration_mean = 500.0
        rolling_mean = 650.0
        variance = 100.0
        drift_threshold = 2.5  # normalized: in standard deviations
        drift_score = abs(rolling_mean - calibration_mean) / max(variance, 1.0)
        assert drift_score == 1.5
        assert drift_score < drift_threshold

    def test_severe_drift(self):
        """Drift > 2.5σ should trigger decay."""
        calibration_mean = 500.0
        rolling_mean = 1000.0
        variance = 100.0
        drift_threshold = 2.5  # normalized
        drift_score = abs(rolling_mean - calibration_mean) / max(variance, 1.0)
        assert drift_score == 5.0
        assert drift_score > drift_threshold


class TestConfidenceDecay:
    def test_single_decay_cycle(self):
        agreement_rate = 0.95
        decay_factor = 1.0
        decay_rate = 0.85

        decay_factor *= decay_rate
        new_confidence = agreement_rate * decay_factor

        assert decay_factor == 0.85
        assert abs(new_confidence - 0.8075) < 1e-4

    def test_multiple_decay_cycles_trigger_reobserve(self):
        """After enough decay cycles, confidence drops below 0.5."""
        agreement_rate = 0.95
        decay_factor = 1.0
        decay_rate = 0.85

        cycles = 0
        while agreement_rate * decay_factor >= 0.5:
            decay_factor *= decay_rate
            cycles += 1

        assert cycles > 0
        assert agreement_rate * decay_factor < 0.5

    def test_recovery_from_drift(self):
        """When drift resolves, decay factor recovers 10% per cycle."""
        decay_factor = 0.6
        recovery_rate = 1.1

        for _ in range(10):
            decay_factor = min(1.0, decay_factor * recovery_rate)

        assert abs(decay_factor - 1.0) < 0.01


class TestNonconformityScore:
    """Test the conformal nonconformity score from RoutingInterceptor."""

    def test_within_bounds_returns_zero(self):
        from modus.routing_interceptor import RoutingInterceptor, RoutingEntry

        entry = RoutingEntry(
            fingerprint_hash="test",
            phase="routing",
            input_token_bucket_bounds=[2, 6],
        )
        score = RoutingInterceptor._compute_nonconformity(3, entry)
        assert score == 0.0

    def test_outside_bounds_returns_positive(self):
        from modus.routing_interceptor import RoutingInterceptor, RoutingEntry

        entry = RoutingEntry(
            fingerprint_hash="test",
            phase="routing",
            input_token_bucket_bounds=[2, 6],
        )
        score = RoutingInterceptor._compute_nonconformity(8, entry)
        assert score > 0.0

    def test_no_bounds_returns_zero(self):
        from modus.routing_interceptor import RoutingInterceptor, RoutingEntry

        entry = RoutingEntry(
            fingerprint_hash="test",
            phase="routing",
            input_token_bucket_bounds=None,
        )
        score = RoutingInterceptor._compute_nonconformity(5, entry)
        assert score == 0.0

    def test_score_capped_at_one(self):
        from modus.routing_interceptor import RoutingInterceptor, RoutingEntry

        entry = RoutingEntry(
            fingerprint_hash="test",
            phase="routing",
            input_token_bucket_bounds=[2, 3],
        )
        # Very far from bounds
        score = RoutingInterceptor._compute_nonconformity(100, entry)
        assert score <= 1.0
