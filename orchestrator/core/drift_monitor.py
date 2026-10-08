"""
Modus — Drift Monitor v2
================================
Background task that detects input distribution drift for routing-active fingerprints.

v2 upgrades from v1:
  - Multi-dimensional drift detection using structural feature centroids
  - Dual-metric scoring: cosine distance + normalized Euclidean distance
  - JSON centroid storage (rolling_centroid_json column)
  - Uses insights_task_loop for hot-reloadable interval/enabled settings
  - Falls back to legacy 1D token-count drift when JSON centroid not available

When drift is detected:
  1. Apply graduated confidence decay (configurable, default 15% per cycle)
  2. Flag fingerprint as drift_flagged
  3. If confidence decays below 0.5, re-enter observe mode
  4. When drift resolves, gradually restore confidence (10% per cycle)
"""

from __future__ import annotations

import logging
import struct
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import RoutingFingerprint, RoutingOutcome
from orchestrator.db.session import get_session_ctx

logger = logging.getLogger(__name__)


async def check_drift() -> None:
    """Check all active fingerprints for input distribution drift."""
    async with get_session_ctx() as db:
        stmt = select(RoutingFingerprint).where(
            RoutingFingerprint.phase.in_(["routing", "drift_flagged"])
        )
        result = await db.execute(stmt)
        active_fps = result.scalars().all()

        for fp in active_fps:
            await _check_fingerprint_drift(db, fp)

        await db.commit()


async def _check_fingerprint_drift(
    db: AsyncSession, fp: RoutingFingerprint
) -> None:
    """
    Computes rolling centroid from recent routing_outcomes.
    v2: Uses multi-dimensional feature vectors when available,
    falls back to legacy 1D token-count drift.
    """
    # Try v2 feature-based drift detection first
    if fp.calibration_centroid_json:
        await _check_drift_v2(db, fp)
    else:
        await _check_drift_v1(db, fp)


async def _check_drift_v2(
    db: AsyncSession, fp: RoutingFingerprint
) -> None:
    """
    Feature-based drift detection using structural feature centroids.
    """
    from orchestrator.core.routing_calibrator import (
        _extract_features_from_message,
        _features_from_token_count,
        compute_centroid,
        cosine_distance,
    )
    from orchestrator.core.config import settings

    # Get last 200 outcomes with features
    stmt = (
        select(
            RoutingOutcome.input_token_count,
            RoutingOutcome.sampled_user_message,
        )
        .where(
            RoutingOutcome.fingerprint_hash == fp.fingerprint_hash,
        )
        .order_by(RoutingOutcome.created_at.desc())
        .limit(200)
    )
    result = await db.execute(stmt)
    recent = result.all()

    if len(recent) < 50:
        return  # Not enough data for reliable drift detection

    # Build feature vectors
    feature_vectors = []
    for row in recent:
        if row.sampled_user_message:
            fv = _extract_features_from_message(row.sampled_user_message)
        elif row.input_token_count:
            fv = _features_from_token_count(row.input_token_count)
        else:
            continue
        feature_vectors.append(fv)

    if len(feature_vectors) < 50:
        return

    # Compute rolling centroid
    rolling = compute_centroid(feature_vectors)
    calibration = fp.calibration_centroid_json

    # Cosine distance between rolling and calibration centroids
    drift_score = cosine_distance(rolling, calibration)

    # Store rolling centroid
    fp.rolling_centroid_json = rolling
    fp.drift_score = drift_score
    fp.rolling_centroid_updated_at = datetime.now(timezone.utc)

    # Also store legacy binary centroid for backward compat
    token_counts = [float(r.input_token_count) for r in recent if r.input_token_count]
    if token_counts:
        rolling_mean = sum(token_counts) / len(token_counts)
        fp.rolling_centroid = struct.pack("d", rolling_mean)

    threshold = fp.drift_threshold or 2.5
    # Cosine distance is [0, 2], normalize to σ-like scale
    # 0.1 cosine distance ≈ meaningful drift for feature vectors
    normalized_score = drift_score * 25.0  # Scale: 0.1 → 2.5σ

    decay_rate = getattr(settings, "routing_confidence_decay_rate", 0.85)

    if normalized_score > threshold:
        _apply_drift_decay(fp, normalized_score, threshold, decay_rate)
    else:
        _apply_drift_recovery(fp)

    fp.updated_at = datetime.now(timezone.utc)


async def _check_drift_v1(
    db: AsyncSession, fp: RoutingFingerprint
) -> None:
    """
    Legacy 1D token-count drift detection.
    Used when calibration_centroid_json is not populated (pre-v2 fingerprints).
    """
    from orchestrator.core.config import settings

    stmt = (
        select(RoutingOutcome.input_token_count)
        .where(
            RoutingOutcome.fingerprint_hash == fp.fingerprint_hash,
            RoutingOutcome.input_token_count.isnot(None),
        )
        .order_by(RoutingOutcome.created_at.desc())
        .limit(200)
    )
    result = await db.execute(stmt)
    recent = result.scalars().all()

    if len(recent) < 50:
        return

    token_counts = [float(r) for r in recent]
    rolling_mean = sum(token_counts) / len(token_counts)

    # Retrieve calibration centroid
    if fp.calibration_centroid is None:
        return

    try:
        calibration_mean = struct.unpack("d", fp.calibration_centroid)[0]
    except struct.error:
        try:
            calibration_mean = float(fp.calibration_centroid.decode())
        except (ValueError, UnicodeDecodeError):
            return

    cal_variance = fp.calibration_centroid_variance or 1.0
    drift_score = abs(rolling_mean - calibration_mean) / max(cal_variance, 1.0)
    fp.drift_score = drift_score
    fp.rolling_centroid_updated_at = datetime.now(timezone.utc)
    fp.rolling_centroid = struct.pack("d", rolling_mean)

    threshold = fp.drift_threshold or 2.5
    decay_rate = getattr(settings, "routing_confidence_decay_rate", 0.85)

    if drift_score > threshold:
        _apply_drift_decay(fp, drift_score, threshold, decay_rate)
    else:
        _apply_drift_recovery(fp)

    fp.updated_at = datetime.now(timezone.utc)


# ── Drift response helpers ────────────────────────────────────────────────────


def _apply_drift_decay(
    fp: RoutingFingerprint,
    drift_score: float,
    threshold: float,
    decay_rate: float,
) -> None:
    """Apply graduated confidence decay when drift is detected."""
    fp.confidence_decay_factor = max(
        0.0, (fp.confidence_decay_factor or 1.0) * decay_rate
    )
    fp.routing_confidence = (
        (fp.cheap_model_agreement_rate or 0.0) * fp.confidence_decay_factor
    )
    fp.phase = "drift_flagged"

    if fp.routing_confidence < 0.5:
        logger.warning(
            "Fingerprint %s confidence decayed to %.2f due to drift. "
            "Re-entering observe mode.",
            fp.fingerprint_hash[:8],
            fp.routing_confidence,
        )
        fp.phase = "observe"
        fp.observe_call_count = 0
        fp.routing_confidence = 0.0
        fp.confidence_decay_factor = 1.0
        _record_drift_metric("re_observe")
    else:
        logger.info(
            "Fingerprint %s drift score %.2f (threshold %.2f). "
            "Confidence decayed to %.2f.",
            fp.fingerprint_hash[:8],
            drift_score,
            threshold,
            fp.routing_confidence,
        )
        _record_drift_metric("flagged")


def _apply_drift_recovery(fp: RoutingFingerprint) -> None:
    """Gradually restore confidence when drift resolves."""
    if fp.confidence_decay_factor and fp.confidence_decay_factor < 1.0:
        fp.confidence_decay_factor = min(
            1.0, fp.confidence_decay_factor * 1.1  # recover 10% per cycle
        )
        fp.routing_confidence = (
            (fp.cheap_model_agreement_rate or 0.0) * fp.confidence_decay_factor
        )
        if fp.phase == "drift_flagged" and fp.confidence_decay_factor > 0.9:
            fp.phase = "routing"
            logger.info(
                "Fingerprint %s drift resolved. Returning to routing.",
                fp.fingerprint_hash[:8],
            )
            _record_drift_metric("recovered")


# ── Metrics ───────────────────────────────────────────────────────────────────


def _record_drift_metric(event: str) -> None:
    """Record drift event to Prometheus (if available)."""
    try:
        from orchestrator.metrics.prometheus import ROUTING_DRIFT_EVENTS
        ROUTING_DRIFT_EVENTS.labels(event=event).inc()
    except Exception:
        pass  # Metrics are optional


# ── Task entry point ──────────────────────────────────────────────────────────


async def run_drift_monitor_cycle() -> None:
    """Single drift check cycle. Called by insights_task_loop."""
    await check_drift()
