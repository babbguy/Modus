"""
Modus — Routing Calibrator v2
=====================================
Background task that manages fingerprint lifecycle:
  observe → calibrating → routing (or excluded)

v2 upgrades from v1:
  - Multi-dimensional structural feature centroids (12 features from SDK)
    instead of 1D token-count mean
  - Feature-vector cosine distance for conformal threshold computation
  - JSON-serialized centroid storage (calibration_centroid_json column)
  - Uses insights_task_loop for hot-reloadable interval/enabled settings
  - Sampled user messages used to extract structural features during calibration
  - Prometheus metrics for calibration events

Steps per fingerprint:
  1. Promote observe → calibrating when call count hits threshold
  2. Run offline consistency calibration from routing_outcomes data
  3. Compute multi-dimensional conformal thresholds
  4. Promote calibrating → routing when agreement rate passes target
  5. Monitor live routing outcomes and demote if misroute rate exceeds budget
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import RoutingFingerprint, RoutingOutcome
from orchestrator.db.session import get_session_ctx

logger = logging.getLogger(__name__)


# ── Feature vector utilities ──────────────────────────────────────────────────

# Feature keys expected from SDK structural extraction (12 features)
FEATURE_KEYS = [
    "system_prompt_length",
    "user_prompt_length",
    "system_prompt_tokens",
    "user_prompt_tokens",
    "total_tokens",
    "has_json_instruction",
    "has_code_instruction",
    "has_classification",
    "has_extraction",
    "prompt_ratio",
    "system_word_count",
    "user_word_count",
]


def _extract_features_from_message(message: str) -> dict:
    """
    Extract structural features from a sampled user message.
    This runs server-side during calibration (not at request time).
    Returns the same 12-feature dict as SDK's extract_structural_features.
    """
    import re

    # We only have the user message, not the system prompt.
    # Use empty string for system prompt features.
    sys_text = ""
    usr_text = message or ""
    sys_len = len(sys_text)
    usr_len = len(usr_text)

    return {
        "system_prompt_length": sys_len,
        "user_prompt_length": usr_len,
        "system_prompt_tokens": max(1, sys_len // 4),
        "user_prompt_tokens": max(1, usr_len // 4),
        "total_tokens": max(1, (sys_len + usr_len) // 4),
        "has_json_instruction": 1 if re.search(r"json|JSON|structured", sys_text) else 0,
        "has_code_instruction": 1 if re.search(r"code|program|function|class|def\s", sys_text) else 0,
        "has_classification": 1 if re.search(r"classif|categoriz|label|tag", sys_text, re.IGNORECASE) else 0,
        "has_extraction": 1 if re.search(r"extract|parse|identify|find", sys_text, re.IGNORECASE) else 0,
        "prompt_ratio": usr_len / max(sys_len, 1),
        "system_word_count": len(sys_text.split()),
        "user_word_count": len(usr_text.split()),
    }


def _features_from_token_count(token_count: int) -> dict:
    """
    Fallback: build a feature vector from just a token count.
    Used when sampled_user_message is not available (legacy outcomes).
    """
    char_est = token_count * 4
    word_est = max(1, token_count * 3 // 4)
    return {
        "system_prompt_length": 0,
        "user_prompt_length": char_est,
        "system_prompt_tokens": 1,
        "user_prompt_tokens": max(1, token_count),
        "total_tokens": max(1, token_count),
        "has_json_instruction": 0,
        "has_code_instruction": 0,
        "has_classification": 0,
        "has_extraction": 0,
        "prompt_ratio": float(char_est),
        "system_word_count": 0,
        "user_word_count": word_est,
    }


def compute_centroid(feature_dicts: list[dict]) -> dict:
    """Compute mean feature vector from a list of feature dicts."""
    n = len(feature_dicts)
    if n == 0:
        return {k: 0.0 for k in FEATURE_KEYS}
    centroid = {k: 0.0 for k in FEATURE_KEYS}
    for fd in feature_dicts:
        for k in FEATURE_KEYS:
            centroid[k] += float(fd.get(k, 0))
    for k in FEATURE_KEYS:
        centroid[k] /= n
    return centroid


def compute_variance(feature_dicts: list[dict], centroid: dict) -> dict:
    """Compute per-feature variance from a list of feature dicts."""
    n = len(feature_dicts)
    if n < 2:
        return {k: 0.0 for k in FEATURE_KEYS}
    var = {k: 0.0 for k in FEATURE_KEYS}
    for fd in feature_dicts:
        for k in FEATURE_KEYS:
            diff = float(fd.get(k, 0)) - centroid[k]
            var[k] += diff * diff
    for k in FEATURE_KEYS:
        var[k] /= n
    return var


def cosine_distance(a: dict, b: dict) -> float:
    """
    Cosine distance between two feature vectors.
    Returns value in [0, 2]. 0 = identical direction, 2 = opposite.
    """
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for k in FEATURE_KEYS:
        va = float(a.get(k, 0))
        vb = float(b.get(k, 0))
        dot += va * vb
        norm_a += va * va
        norm_b += vb * vb
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom < 1e-12:
        return 0.0  # Both near-zero vectors → no distance
    similarity = max(-1.0, min(1.0, dot / denom))
    return 1.0 - similarity


def euclidean_distance_normalized(a: dict, b: dict, variance: dict) -> float:
    """
    Normalized Euclidean distance (Mahalanobis-like with diagonal covariance).
    Each dimension is scaled by its standard deviation.
    """
    total = 0.0
    for k in FEATURE_KEYS:
        va = float(a.get(k, 0))
        vb = float(b.get(k, 0))
        std = max(math.sqrt(float(variance.get(k, 0))), 1e-6)
        total += ((va - vb) / std) ** 2
    return math.sqrt(total / len(FEATURE_KEYS))


# ── Calibration core ─────────────────────────────────────────────────────────


async def calibrate_fingerprints() -> None:
    """Promote observe → calibrating → routing for eligible fingerprints."""
    async with get_session_ctx() as db:
        stmt = select(RoutingFingerprint).where(
            RoutingFingerprint.phase == "observe",
            RoutingFingerprint.observe_call_count >= RoutingFingerprint.observe_threshold,
        )
        result = await db.execute(stmt)
        candidates = result.scalars().all()

        for fp in candidates:
            try:
                await _run_calibration(db, fp)
            except Exception as exc:
                logger.error(
                    "Calibration failed for %s: %s",
                    fp.fingerprint_hash[:8], exc
                )


async def _run_calibration(db: AsyncSession, fp: RoutingFingerprint) -> None:
    """
    Offline calibration for a single fingerprint.

    v2: Uses structural feature vectors extracted from sampled user messages
    (or token-count fallback) to compute multi-dimensional centroids.
    Agreement rate is still based on validator pass rate from outcomes.
    """
    fp.phase = "calibrating"
    await db.commit()

    from orchestrator.core.config import settings

    min_samples = getattr(settings, "routing_min_calibration_samples", 10)

    # Pull recent outcomes for this fingerprint
    stmt = (
        select(
            RoutingOutcome.input_token_count,
            RoutingOutcome.input_token_bucket,
            RoutingOutcome.sampled_user_message,
        )
        .where(
            RoutingOutcome.fingerprint_hash == fp.fingerprint_hash,
            RoutingOutcome.created_at > datetime.now(timezone.utc) - timedelta(days=7),
        )
        .order_by(RoutingOutcome.created_at.desc())
        .limit(100)
    )
    result = await db.execute(stmt)
    recent = result.all()

    if len(recent) < min_samples:
        logger.info(
            "Not enough calibration data for %s (%d samples, need %d), staying in observe",
            fp.fingerprint_hash[:8], len(recent), min_samples
        )
        fp.phase = "observe"
        await db.commit()
        return

    # Build feature vectors from outcomes
    feature_vectors = []
    for row in recent:
        if row.sampled_user_message:
            fv = _extract_features_from_message(row.sampled_user_message)
        elif row.input_token_count:
            fv = _features_from_token_count(row.input_token_count)
        else:
            continue
        feature_vectors.append(fv)

    if len(feature_vectors) < min_samples:
        fp.phase = "observe"
        await db.commit()
        return

    # Compute centroid
    centroid = compute_centroid(feature_vectors)

    # Compute token bucket bounds (still used for lightweight conformal checks in SDK)
    token_counts = [r.input_token_count for r in recent if r.input_token_count]
    if token_counts:
        from modus.routing_interceptor import token_bucket as compute_bucket
        buckets = [compute_bucket(t) for t in token_counts]
        bucket_low = min(buckets)
        bucket_high = max(buckets)
    else:
        bucket_low, bucket_high = 0, 9

    # Conformal threshold: compute distances from centroid, use 95th percentile
    distances = [cosine_distance(fv, centroid) for fv in feature_vectors]
    distances.sort()
    p95_idx = min(len(distances) - 1, int(len(distances) * 0.95))
    conformal_threshold = distances[p95_idx] * 1.2  # 20% margin

    # Legacy 1D variance (for backward-compat drift threshold)
    if token_counts:
        tc_mean = sum(token_counts) / len(token_counts)
        tc_variance = (
            sum((t - tc_mean) ** 2 for t in token_counts) / len(token_counts)
        ) ** 0.5
    else:
        tc_mean = 0.0
        tc_variance = 1.0

    # Agreement rate from validator outcomes
    agreement_rate = await _compute_agreement_rate(db, fp)

    # Cold-start: not enough validated outcomes to justify routing. Hold the
    # fingerprint in 'observe' (it keeps collecting outcomes on the expensive
    # model) rather than routing production traffic on an unproven guess.
    if agreement_rate is None:
        fp.phase = "observe"
        fp.routing_confidence = 0.0
        fp.updated_at = datetime.now(timezone.utc)
        logger.info(
            "Fingerprint %s held in observe: awaiting validated outcomes before routing.",
            fp.fingerprint_hash[:8],
        )
        _record_calibration_metric("held_cold_start", fp.fingerprint_hash)
        await db.commit()
        return

    # Decision: promote if agreement rate meets target
    min_agreement = 1.0 - fp.max_misroute_rate

    if agreement_rate >= min_agreement:
        fp.phase = "routing"
        fp.routing_confidence = agreement_rate
        fp.conformal_threshold = conformal_threshold
        fp.cheap_model_agreement_rate = agreement_rate
        fp.calibration_sample_count = len(feature_vectors)
        fp.calibration_centroid_json = centroid
        fp.calibration_centroid_variance = tc_variance
        fp.drift_threshold = 2.5
        fp.input_token_bucket_bounds = [bucket_low, bucket_high]
        # Legacy binary centroid (backward compat)
        import struct
        fp.calibration_centroid = struct.pack("d", tc_mean)
        logger.info(
            "Fingerprint %s promoted to routing. "
            "Agreement: %.2f%%, Conformal threshold: %.3f, Samples: %d",
            fp.fingerprint_hash[:8],
            agreement_rate * 100,
            conformal_threshold,
            len(feature_vectors),
        )
        _record_calibration_metric("promoted", fp.fingerprint_hash)
    else:
        fp.phase = "excluded"
        fp.routing_confidence = agreement_rate
        logger.info(
            "Fingerprint %s excluded from routing. "
            "Agreement rate %.2f%% below threshold %.2f%%",
            fp.fingerprint_hash[:8],
            agreement_rate * 100,
            min_agreement * 100,
        )
        _record_calibration_metric("excluded", fp.fingerprint_hash)

    fp.updated_at = datetime.now(timezone.utc)
    await db.commit()


async def _compute_agreement_rate(db: AsyncSession, fp: RoutingFingerprint) -> Optional[float]:
    """
    Compute agreement rate from validator pass rate in routing outcomes.
    Returns None on cold-start (fewer than 10 validated outcomes) so the caller
    holds the fingerprint in observe instead of routing on an unproven guess.
    """
    stmt_total = (
        select(func.count())
        .select_from(RoutingOutcome)
        .where(
            RoutingOutcome.fingerprint_hash == fp.fingerprint_hash,
            RoutingOutcome.routed_to.in_(["cheap", "escalated"]),
            RoutingOutcome.validator_passed.isnot(None),
            RoutingOutcome.created_at > datetime.now(timezone.utc) - timedelta(days=7),
        )
    )
    stmt_passed = (
        select(func.count())
        .select_from(RoutingOutcome)
        .where(
            RoutingOutcome.fingerprint_hash == fp.fingerprint_hash,
            RoutingOutcome.routed_to.in_(["cheap", "escalated"]),
            RoutingOutcome.validator_passed == True,  # noqa: E712
            RoutingOutcome.created_at > datetime.now(timezone.utc) - timedelta(days=7),
        )
    )

    total = (await db.execute(stmt_total)).scalar() or 0
    passed = (await db.execute(stmt_passed)).scalar() or 0

    if total >= 10:
        return passed / total if total > 0 else 0.0

    # Insufficient validated outcomes — signal cold-start. The caller must NOT
    # promote to routing on a guess; the old code returned an optimistic 0.95
    # here, which routed production traffic before any real evidence existed.
    return None


# ── Outcome evaluation ────────────────────────────────────────────────────────


async def evaluate_routing_outcomes() -> None:
    """
    Read recent routing_outcomes and adjust fingerprint confidence/phase
    based on observed misroute rates.
    """
    async with get_session_ctx() as db:
        stmt = select(RoutingFingerprint).where(
            RoutingFingerprint.phase == "routing"
        )
        result = await db.execute(stmt)
        active_fps = result.scalars().all()

        window_start = datetime.now(timezone.utc) - timedelta(hours=24)

        for fp in active_fps:
            stmt_total = (
                select(func.count())
                .select_from(RoutingOutcome)
                .where(
                    RoutingOutcome.fingerprint_hash == fp.fingerprint_hash,
                    RoutingOutcome.routed_to.in_(["cheap", "escalated"]),
                    RoutingOutcome.created_at > window_start,
                )
            )
            stmt_escalations = (
                select(func.count())
                .select_from(RoutingOutcome)
                .where(
                    RoutingOutcome.fingerprint_hash == fp.fingerprint_hash,
                    RoutingOutcome.routed_to == "escalated",
                    RoutingOutcome.created_at > window_start,
                )
            )

            total = (await db.execute(stmt_total)).scalar() or 0
            escalations = (await db.execute(stmt_escalations)).scalar() or 0

            if total < 20:
                continue  # Not enough data for reliable rate

            observed_misroute_rate = escalations / total

            if observed_misroute_rate > fp.max_misroute_rate * 1.5:
                logger.warning(
                    "Fingerprint %s misroute rate %.2f%% exceeds budget. Demoting to observe.",
                    fp.fingerprint_hash[:8],
                    observed_misroute_rate * 100,
                )
                fp.phase = "observe"
                fp.observe_call_count = 0
                fp.routing_confidence = 0.0
                fp.updated_at = datetime.now(timezone.utc)
                _record_calibration_metric("demoted", fp.fingerprint_hash)

            # Update cumulative counters
            fp.total_routed_calls = (fp.total_routed_calls or 0) + total
            fp.total_escalations = (fp.total_escalations or 0) + escalations

        await db.commit()


# ── Metrics ───────────────────────────────────────────────────────────────────


def _record_calibration_metric(event: str, fingerprint_hash: str) -> None:
    """Record calibration event to Prometheus (if available)."""
    try:
        from orchestrator.metrics.prometheus import ROUTING_CALIBRATION_EVENTS
        ROUTING_CALIBRATION_EVENTS.labels(event=event).inc()
    except Exception:
        pass  # Metrics are optional


# ── Task entry point ──────────────────────────────────────────────────────────


async def run_calibrator_cycle() -> None:
    """Single calibration cycle. Called by insights_task_loop."""
    await calibrate_fingerprints()
    await evaluate_routing_outcomes()
