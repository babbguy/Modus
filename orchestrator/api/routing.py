"""
Modus — Routing Intelligence API Router
===============================================
GET  /routing/fingerprints                     List all fingerprints
GET  /routing/fingerprints/{hash}              Single fingerprint detail
GET  /routing/outcomes                         Routing outcome summary
GET  /routing/savings                          Aggregate cost savings
GET  /routing/summary                          Dashboard summary stats
GET  /routing/savings-over-time                Daily savings for charts
POST /routing/fingerprints/{hash}/reset        Reset fingerprint to observe
POST /routing/fingerprints/{hash}/exclude      Exclude from routing
PATCH /routing/fingerprints/{hash}             Update allow_routing/force_model
POST /routing/outcomes                         Log a routing outcome (from SDK)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.ingest import _verify_app_key
from orchestrator.core.auth import get_app_identity, get_identity
from orchestrator.db.models import RoutingFingerprint, RoutingOutcome
from orchestrator.db.session import get_session
from orchestrator.metrics.prometheus import (
    ROUTING_DECISIONS_TOTAL,
    ROUTING_COST_SAVED_TOTAL,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Response models ──────────────────────────────────────────────────────────


class RoutingFingerprintResponse(BaseModel):
    id: str
    app_id: str
    fingerprint_hash: str
    system_prompt_hash: str
    call_site_id: Optional[str]
    task_type: Optional[str]
    phase: str
    observe_call_count: int
    observe_threshold: int
    routing_confidence: float
    conformal_threshold: Optional[float]
    cheap_model: Optional[str]
    expensive_model: Optional[str]
    cheap_model_agreement_rate: Optional[float]
    calibration_sample_count: int
    drift_score: float
    drift_threshold: Optional[float]
    confidence_decay_factor: float
    force_model: Optional[str]
    allow_routing: bool
    max_misroute_rate: float
    total_routed_calls: int
    total_escalations: int
    total_validator_failures: int
    input_token_bucket_bounds: Optional[list]
    created_at: datetime
    updated_at: datetime


class RoutingOutcomeSummary(BaseModel):
    fingerprint_hash: str
    total_calls: int
    cheap_calls: int
    expensive_calls: int
    escalated_calls: int
    total_cost_saved: str  # Decimal as string for precision
    avg_cheap_latency_ms: Optional[float]
    avg_expensive_latency_ms: Optional[float]
    validator_pass_rate: Optional[float]


class RoutingSavingsResponse(BaseModel):
    total_cost_saved: str  # Decimal as string for precision
    total_routed_calls: int
    total_escalations: int
    active_fingerprints: int
    observe_fingerprints: int
    avg_routing_confidence: Optional[float]


async def _get_fingerprint(
    db: AsyncSession, fingerprint_hash: str
) -> Optional[RoutingFingerprint]:
    result = await db.execute(
        select(RoutingFingerprint).where(
            RoutingFingerprint.fingerprint_hash == fingerprint_hash
        )
    )
    return result.scalar_one_or_none()


class RoutingOutcomeIn(BaseModel):
    """Routing outcome from SDK."""
    fingerprint_hash: str = Field(..., max_length=64)
    # Accepted for backward compatibility and ignored: the app is always the
    # one the X-Modus-APIKey credential authenticates.
    app_id: Optional[str] = Field(None, max_length=128)
    request_id: Optional[str] = Field(None, max_length=36)
    routed_to: str = Field(..., max_length=16)
    cheap_model: Optional[str] = Field(None, max_length=128)
    expensive_model: Optional[str] = Field(None, max_length=128)
    input_token_count: Optional[int] = Field(None, ge=0)
    output_token_count: Optional[int] = Field(None, ge=0)
    input_token_bucket: Optional[int] = None
    structural_check_passed: Optional[bool] = None
    conformal_check_passed: Optional[bool] = None
    validator_passed: Optional[bool] = None
    escalation_reason: Optional[str] = Field(None, max_length=64)
    cheap_latency_ms: Optional[int] = None
    expensive_latency_ms: Optional[int] = None
    cost_cheap: Optional[float] = None
    cost_expensive: Optional[float] = None
    cost_saved: Optional[float] = None
    nonconformity_score: Optional[float] = None
    system_prompt_hash: Optional[str] = Field(None, max_length=64)


class StatusResponse(BaseModel):
    status: str


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get(
    "/routing/fingerprints",
    response_model=list[RoutingFingerprintResponse],
    summary="List all routing fingerprints",
)
async def list_fingerprints(
    app_id: Optional[UUID] = None,
    phase: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[RoutingFingerprintResponse]:
    stmt = select(RoutingFingerprint)
    if app_id:
        stmt = stmt.where(RoutingFingerprint.app_id == str(app_id))
    if phase:
        stmt = stmt.where(RoutingFingerprint.phase == phase)
    stmt = stmt.order_by(RoutingFingerprint.updated_at.desc())
    stmt = stmt.limit(limit).offset(offset)

    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [
        RoutingFingerprintResponse(
            id=fp.id,
            app_id=fp.app_id,
            fingerprint_hash=fp.fingerprint_hash,
            system_prompt_hash=fp.system_prompt_hash,
            call_site_id=fp.call_site_id,
            task_type=fp.task_type,
            phase=fp.phase,
            observe_call_count=fp.observe_call_count,
            observe_threshold=fp.observe_threshold,
            routing_confidence=fp.routing_confidence,
            conformal_threshold=fp.conformal_threshold,
            cheap_model=fp.cheap_model,
            expensive_model=fp.expensive_model,
            cheap_model_agreement_rate=fp.cheap_model_agreement_rate,
            calibration_sample_count=fp.calibration_sample_count,
            drift_score=fp.drift_score,
            drift_threshold=fp.drift_threshold,
            confidence_decay_factor=fp.confidence_decay_factor,
            force_model=fp.force_model,
            allow_routing=fp.allow_routing,
            max_misroute_rate=fp.max_misroute_rate,
            total_routed_calls=fp.total_routed_calls,
            total_escalations=fp.total_escalations,
            total_validator_failures=fp.total_validator_failures,
            input_token_bucket_bounds=fp.input_token_bucket_bounds,
            created_at=fp.created_at,
            updated_at=fp.updated_at,
        )
        for fp in rows
    ]


@router.get(
    "/routing/fingerprints/{fingerprint_hash}",
    response_model=RoutingFingerprintResponse,
    summary="Get a single fingerprint",
)
async def get_fingerprint(
    fingerprint_hash: str,
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> RoutingFingerprintResponse:
    stmt = select(RoutingFingerprint).where(
        RoutingFingerprint.fingerprint_hash == fingerprint_hash
    )
    result = await db.execute(stmt)
    fp = result.scalar_one_or_none()
    if not fp:
        raise HTTPException(status_code=404, detail="Fingerprint not found")
    return RoutingFingerprintResponse(
        id=fp.id,
        app_id=fp.app_id,
        fingerprint_hash=fp.fingerprint_hash,
        system_prompt_hash=fp.system_prompt_hash,
        call_site_id=fp.call_site_id,
        task_type=fp.task_type,
        phase=fp.phase,
        observe_call_count=fp.observe_call_count,
        observe_threshold=fp.observe_threshold,
        routing_confidence=fp.routing_confidence,
        conformal_threshold=fp.conformal_threshold,
        cheap_model=fp.cheap_model,
        expensive_model=fp.expensive_model,
        cheap_model_agreement_rate=fp.cheap_model_agreement_rate,
        calibration_sample_count=fp.calibration_sample_count,
        drift_score=fp.drift_score,
        drift_threshold=fp.drift_threshold,
        confidence_decay_factor=fp.confidence_decay_factor,
        force_model=fp.force_model,
        allow_routing=fp.allow_routing,
        max_misroute_rate=fp.max_misroute_rate,
        total_routed_calls=fp.total_routed_calls,
        total_escalations=fp.total_escalations,
        total_validator_failures=fp.total_validator_failures,
        input_token_bucket_bounds=fp.input_token_bucket_bounds,
        created_at=fp.created_at,
        updated_at=fp.updated_at,
    )


@router.get(
    "/routing/outcomes",
    response_model=list[RoutingOutcomeSummary],
    summary="Routing outcome summary per fingerprint",
)
async def list_outcome_summaries(
    fingerprint_hash: Optional[str] = None,
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[RoutingOutcomeSummary]:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Build base filter
    filters = [RoutingOutcome.created_at > since]
    if fingerprint_hash:
        filters.append(RoutingOutcome.fingerprint_hash == fingerprint_hash)

    stmt = (
        select(
            RoutingOutcome.fingerprint_hash,
            func.count().label("total_calls"),
            func.sum(func.cast(RoutingOutcome.routed_to == "cheap", Integer)).label("cheap_calls"),
            func.sum(func.cast(RoutingOutcome.routed_to == "expensive", Integer)).label("expensive_calls"),
            func.sum(func.cast(RoutingOutcome.routed_to == "escalated", Integer)).label("escalated_calls"),
            func.coalesce(func.sum(RoutingOutcome.cost_saved), 0.0).label("total_cost_saved"),
            func.avg(RoutingOutcome.cheap_latency_ms).label("avg_cheap_latency_ms"),
            func.avg(RoutingOutcome.expensive_latency_ms).label("avg_expensive_latency_ms"),
        )
        .where(*filters)
        .group_by(RoutingOutcome.fingerprint_hash)
        .order_by(func.sum(RoutingOutcome.cost_saved).desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(stmt)
    rows = result.all()

    summaries = []
    for row in rows:
        total = row.total_calls or 0
        cheap = row.cheap_calls or 0
        escalated = row.escalated_calls or 0
        routed = cheap + escalated
        pass_rate = cheap / routed if routed > 0 else None

        summaries.append(RoutingOutcomeSummary(
            fingerprint_hash=row.fingerprint_hash,
            total_calls=total,
            cheap_calls=cheap,
            expensive_calls=row.expensive_calls or 0,
            escalated_calls=escalated,
            total_cost_saved=str(row.total_cost_saved or 0),
            avg_cheap_latency_ms=float(row.avg_cheap_latency_ms) if row.avg_cheap_latency_ms else None,
            avg_expensive_latency_ms=float(row.avg_expensive_latency_ms) if row.avg_expensive_latency_ms else None,
            validator_pass_rate=pass_rate,
        ))

    return summaries


@router.get(
    "/routing/savings",
    response_model=RoutingSavingsResponse,
    summary="Aggregate routing cost savings",
)
async def get_savings(
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> RoutingSavingsResponse:
    # Aggregate from fingerprints
    fp_stmt = select(
        func.count().label("total"),
        func.sum(func.cast(RoutingFingerprint.phase == "routing", Integer)).label("active"),
        func.sum(func.cast(RoutingFingerprint.phase == "observe", Integer)).label("observe"),
        func.sum(RoutingFingerprint.total_routed_calls).label("routed"),
        func.sum(RoutingFingerprint.total_escalations).label("escalated"),
        func.avg(
            func.nullif(RoutingFingerprint.routing_confidence, 0.0)
        ).label("avg_conf"),
    ).select_from(RoutingFingerprint)

    fp_result = await db.execute(fp_stmt)
    fp_row = fp_result.one()

    # Total cost saved from outcomes
    cost_stmt = select(
        func.coalesce(func.sum(RoutingOutcome.cost_saved), 0.0)
    ).select_from(RoutingOutcome)
    cost_result = await db.execute(cost_stmt)
    total_saved = cost_result.scalar() or 0.0

    return RoutingSavingsResponse(
        total_cost_saved=str(total_saved),
        total_routed_calls=fp_row.routed or 0,
        total_escalations=fp_row.escalated or 0,
        active_fingerprints=fp_row.active or 0,
        observe_fingerprints=fp_row.observe or 0,
        avg_routing_confidence=float(fp_row.avg_conf) if fp_row.avg_conf else None,
    )


@router.post(
    "/routing/fingerprints/{fingerprint_hash}/reset",
    response_model=StatusResponse,
    summary="Reset fingerprint to observe mode",
)
async def reset_fingerprint(
    fingerprint_hash: str,
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> StatusResponse:
    stmt = select(RoutingFingerprint).where(
        RoutingFingerprint.fingerprint_hash == fingerprint_hash
    )
    result = await db.execute(stmt)
    fp = result.scalar_one_or_none()
    if not fp:
        raise HTTPException(status_code=404, detail="Fingerprint not found")

    fp.phase = "observe"
    fp.observe_call_count = 0
    fp.routing_confidence = 0.0
    fp.confidence_decay_factor = 1.0
    fp.drift_score = 0.0
    fp.updated_at = datetime.now(timezone.utc)

    logger.info("Fingerprint %s reset to observe by %s", fingerprint_hash[:8], identity)
    return StatusResponse(status="reset_to_observe")


@router.post(
    "/routing/fingerprints/{fingerprint_hash}/exclude",
    response_model=StatusResponse,
    summary="Exclude fingerprint from routing",
)
async def exclude_fingerprint(
    fingerprint_hash: str,
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> StatusResponse:
    stmt = select(RoutingFingerprint).where(
        RoutingFingerprint.fingerprint_hash == fingerprint_hash
    )
    result = await db.execute(stmt)
    fp = result.scalar_one_or_none()
    if not fp:
        raise HTTPException(status_code=404, detail="Fingerprint not found")

    fp.phase = "excluded"
    fp.allow_routing = False
    fp.updated_at = datetime.now(timezone.utc)

    logger.info("Fingerprint %s excluded by %s", fingerprint_hash[:8], identity)
    return StatusResponse(status="excluded")


# ── Dashboard endpoints ───────────────────────────────────────────────────────


class RoutingSummaryResponse(BaseModel):
    total_fingerprints: int
    routing_active: int
    routing_observe: int
    routing_excluded: int
    drift_flagged: int
    total_cost_saved_usd: float
    cost_saved_30d_usd: float
    total_routed_calls: int
    total_escalations: int
    escalation_rate: float
    avg_routing_confidence: Optional[float]


@router.get(
    "/routing/summary",
    response_model=RoutingSummaryResponse,
    summary="Aggregate routing stats for dashboard",
)
async def get_routing_summary(
    app_id: Optional[UUID] = None,
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> RoutingSummaryResponse:
    # Phase counts
    fp_filters = []
    if app_id:
        fp_filters.append(RoutingFingerprint.app_id == str(app_id))

    fp_stmt = select(
        func.count().label("total"),
        func.sum(func.cast(RoutingFingerprint.phase == "routing", Integer)).label("active"),
        func.sum(func.cast(RoutingFingerprint.phase == "observe", Integer)).label("observe"),
        func.sum(func.cast(RoutingFingerprint.phase == "excluded", Integer)).label("excluded"),
        func.sum(func.cast(RoutingFingerprint.phase == "drift_flagged", Integer)).label("drift"),
        func.sum(RoutingFingerprint.total_routed_calls).label("routed"),
        func.sum(RoutingFingerprint.total_escalations).label("escalated"),
        func.avg(func.nullif(RoutingFingerprint.routing_confidence, 0.0)).label("avg_conf"),
    ).select_from(RoutingFingerprint)
    if fp_filters:
        fp_stmt = fp_stmt.where(*fp_filters)

    fp_result = await db.execute(fp_stmt)
    fp = fp_result.one()

    # Total cost saved (all time)
    cost_filters = []
    if app_id:
        cost_filters.append(RoutingOutcome.app_id == str(app_id))
    cost_stmt = select(
        func.coalesce(func.sum(RoutingOutcome.cost_saved), 0.0)
    ).select_from(RoutingOutcome)
    if cost_filters:
        cost_stmt = cost_stmt.where(*cost_filters)
    total_saved = (await db.execute(cost_stmt)).scalar() or 0.0

    # Cost saved last 30 days
    since_30d = datetime.now(timezone.utc) - timedelta(days=30)
    cost_30d_filters = [RoutingOutcome.created_at > since_30d]
    if app_id:
        cost_30d_filters.append(RoutingOutcome.app_id == str(app_id))
    cost_30d_stmt = select(
        func.coalesce(func.sum(RoutingOutcome.cost_saved), 0.0)
    ).select_from(RoutingOutcome).where(*cost_30d_filters)
    saved_30d = (await db.execute(cost_30d_stmt)).scalar() or 0.0

    total_routed = fp.routed or 0
    total_escalations = fp.escalated or 0
    esc_rate = total_escalations / total_routed if total_routed > 0 else 0.0

    return RoutingSummaryResponse(
        total_fingerprints=fp.total or 0,
        routing_active=fp.active or 0,
        routing_observe=fp.observe or 0,
        routing_excluded=fp.excluded or 0,
        drift_flagged=fp.drift or 0,
        total_cost_saved_usd=float(total_saved),
        cost_saved_30d_usd=float(saved_30d),
        total_routed_calls=total_routed,
        total_escalations=total_escalations,
        escalation_rate=round(esc_rate, 4),
        avg_routing_confidence=float(fp.avg_conf) if fp.avg_conf else None,
    )


class DailySavingsRow(BaseModel):
    date: str
    cost_saved_usd: float
    routed_calls: int
    escalations: int


@router.get(
    "/routing/savings-over-time",
    response_model=list[DailySavingsRow],
    summary="Daily cost savings for chart rendering",
)
async def get_savings_over_time(
    days: int = Query(30, ge=1, le=90),
    app_id: Optional[UUID] = None,
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[DailySavingsRow]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    filters = [RoutingOutcome.created_at > since]
    if app_id:
        filters.append(RoutingOutcome.app_id == str(app_id))

    # Use DATE() extraction — works on both PostgreSQL and SQLite
    date_col = func.date(RoutingOutcome.created_at)

    stmt = (
        select(
            date_col.label("day"),
            func.coalesce(func.sum(RoutingOutcome.cost_saved), 0.0).label("saved"),
            func.sum(func.cast(
                RoutingOutcome.routed_to.in_(["cheap", "cheap_provisional"]), Integer
            )).label("routed"),
            func.sum(func.cast(
                RoutingOutcome.routed_to == "escalated", Integer
            )).label("escalated"),
        )
        .where(*filters)
        .group_by(date_col)
        .order_by(date_col)
    )

    result = await db.execute(stmt)
    rows = result.all()

    return [
        DailySavingsRow(
            date=str(row.day),
            cost_saved_usd=float(row.saved or 0),
            routed_calls=int(row.routed or 0),
            escalations=int(row.escalated or 0),
        )
        for row in rows
    ]


class FingerprintPatchIn(BaseModel):
    allow_routing: Optional[bool] = None
    force_model: Optional[str] = Field(None, max_length=128)


@router.patch(
    "/routing/fingerprints/{fingerprint_hash}",
    response_model=RoutingFingerprintResponse,
    summary="Update routing settings for a fingerprint",
)
async def patch_fingerprint(
    fingerprint_hash: str,
    body: FingerprintPatchIn,
    identity=Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> RoutingFingerprintResponse:
    stmt = select(RoutingFingerprint).where(
        RoutingFingerprint.fingerprint_hash == fingerprint_hash
    )
    result = await db.execute(stmt)
    fp = result.scalar_one_or_none()
    if not fp:
        raise HTTPException(status_code=404, detail="Fingerprint not found")

    if body.allow_routing is not None:
        fp.allow_routing = body.allow_routing
    if body.force_model is not None:
        fp.force_model = body.force_model if body.force_model else None

    fp.updated_at = datetime.now(timezone.utc)

    logger.info(
        "Fingerprint %s patched by %s: allow_routing=%s force_model=%s",
        fingerprint_hash[:8], identity, fp.allow_routing, fp.force_model,
    )
    return RoutingFingerprintResponse(
        id=fp.id,
        app_id=fp.app_id,
        fingerprint_hash=fp.fingerprint_hash,
        system_prompt_hash=fp.system_prompt_hash,
        call_site_id=fp.call_site_id,
        task_type=fp.task_type,
        phase=fp.phase,
        observe_call_count=fp.observe_call_count,
        observe_threshold=fp.observe_threshold,
        routing_confidence=fp.routing_confidence,
        conformal_threshold=fp.conformal_threshold,
        cheap_model=fp.cheap_model,
        expensive_model=fp.expensive_model,
        cheap_model_agreement_rate=fp.cheap_model_agreement_rate,
        calibration_sample_count=fp.calibration_sample_count,
        drift_score=fp.drift_score,
        drift_threshold=fp.drift_threshold,
        confidence_decay_factor=fp.confidence_decay_factor,
        force_model=fp.force_model,
        allow_routing=fp.allow_routing,
        max_misroute_rate=fp.max_misroute_rate,
        total_routed_calls=fp.total_routed_calls,
        total_escalations=fp.total_escalations,
        total_validator_failures=fp.total_validator_failures,
        input_token_bucket_bounds=fp.input_token_bucket_bounds,
        created_at=fp.created_at,
        updated_at=fp.updated_at,
    )


# ── SDK ingest endpoints ──────────────────────────────────────────────────────


@router.post(
    "/routing/outcomes",
    status_code=status.HTTP_201_CREATED,
    summary="Log a routing outcome from SDK",
)
async def log_routing_outcome(
    body: RoutingOutcomeIn,
    _app_key: str = Depends(get_app_identity),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """
    Receives routing outcome data from the SDK agent.
    Also creates/updates the fingerprint if it doesn't exist (observe mode).

    Authenticated with the app credential (X-Modus-APIKey). The outcome is
    always recorded against the authenticated app, never a client-supplied id.
    """
    app = await _verify_app_key(_app_key, db)
    app_uuid = str(app.id)

    fp = await _get_fingerprint(db, body.fingerprint_hash)
    if fp is not None and str(fp.app_id) != app_uuid:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Fingerprint belongs to a different app.",
        )

    if fp is None and body.system_prompt_hash:
        # Create new fingerprint in observe mode
        from orchestrator.core.config import settings
        fp = RoutingFingerprint(
            app_id=app_uuid,
            fingerprint_hash=body.fingerprint_hash,
            system_prompt_hash=body.system_prompt_hash,
            phase="observe",
            cheap_model=getattr(settings, "routing_cheap_model_default", "claude-haiku-4-5-20251001"),
            expensive_model=body.expensive_model,
        )
        db.add(fp)
    elif fp is not None:
        fp.observe_call_count = (fp.observe_call_count or 0) + 1

    # Log the outcome
    outcome = RoutingOutcome(
        fingerprint_hash=body.fingerprint_hash,
        app_id=app_uuid,
        request_id=body.request_id,
        routed_to=body.routed_to,
        cheap_model=body.cheap_model,
        expensive_model=body.expensive_model,
        input_token_count=body.input_token_count,
        output_token_count=body.output_token_count,
        input_token_bucket=body.input_token_bucket,
        structural_check_passed=body.structural_check_passed,
        conformal_check_passed=body.conformal_check_passed,
        validator_passed=body.validator_passed,
        escalation_reason=body.escalation_reason,
        cheap_latency_ms=body.cheap_latency_ms,
        expensive_latency_ms=body.expensive_latency_ms,
        cost_cheap=body.cost_cheap,
        cost_expensive=body.cost_expensive,
        cost_saved=body.cost_saved,
        nonconformity_score=body.nonconformity_score,
    )
    db.add(outcome)

    # Update metrics
    ROUTING_DECISIONS_TOTAL.labels(decision=body.routed_to).inc()
    if body.cost_saved and body.cost_saved > 0:
        ROUTING_COST_SAVED_TOTAL.inc(body.cost_saved)

    return {"status": "logged"}


class RoutingOutcomeBatchIn(BaseModel):
    """Batch of routing outcomes from SDK flush."""
    outcomes: list[dict] = Field(..., max_length=500)


class BatchResponse(BaseModel):
    accepted: int
    errors: int


@router.post(
    "/routing/outcomes/batch",
    response_model=BatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log a batch of routing outcomes from SDK",
)
async def log_routing_outcomes_batch(
    body: RoutingOutcomeBatchIn,
    _app_key: str = Depends(get_app_identity),
    db: AsyncSession = Depends(get_session),
) -> BatchResponse:
    """
    Receives batched routing outcome data from the SDK agent flush cycle.
    Creates/updates fingerprints and logs outcomes.

    Authenticated with the app credential (X-Modus-APIKey). Every outcome is
    recorded against the authenticated app; any ``app_id`` in the payload is
    ignored. Outcomes whose fingerprint belongs to another app are rejected.
    """
    app = await _verify_app_key(_app_key, db)
    app_id = str(app.id)
    accepted = 0
    errors = 0

    for raw in body.outcomes:
        try:
            fingerprint_hash = raw.get("fingerprint_hash", "")
            if not fingerprint_hash:
                errors += 1
                continue

            routed_to = raw.get("routed_to", "unknown")

            fp = await _get_fingerprint(db, fingerprint_hash)
            if fp is not None and str(fp.app_id) != app_id:
                errors += 1
                continue

            if fp is None:
                from orchestrator.core.config import settings
                fp = RoutingFingerprint(
                    app_id=app_id,
                    fingerprint_hash=fingerprint_hash,
                    system_prompt_hash=raw.get("system_prompt_hash", "unknown"),
                    phase="observe",
                    cheap_model=getattr(
                        settings, "routing_cheap_model_default",
                        "claude-haiku-4-5-20251001",
                    ),
                )
                db.add(fp)
            else:
                fp.observe_call_count = (fp.observe_call_count or 0) + 1

            # Log the outcome
            outcome = RoutingOutcome(
                fingerprint_hash=fingerprint_hash,
                app_id=app_id,
                request_id=raw.get("request_id"),
                routed_to=routed_to,
                cheap_model=raw.get("routed_model") or raw.get("cheap_model"),
                expensive_model=raw.get("original_model") or raw.get("expensive_model"),
                input_token_count=raw.get("input_token_count"),
                output_token_count=raw.get("output_token_count"),
                escalation_reason=raw.get("escalation_reason"),
                cheap_latency_ms=raw.get("cheap_latency_ms"),
                expensive_latency_ms=raw.get("expensive_latency_ms"),
                cost_saved=raw.get("cost_saved"),
                sampled_user_message=raw.get("sampled_user_message"),
                layer=raw.get("layer"),
            )
            db.add(outcome)

            # Update metrics
            ROUTING_DECISIONS_TOTAL.labels(decision=routed_to).inc()
            cost_saved = raw.get("cost_saved")
            if cost_saved and cost_saved > 0:
                ROUTING_COST_SAVED_TOTAL.inc(cost_saved)

            accepted += 1
        except Exception as exc:
            logger.debug("Batch outcome error: %s", exc)
            errors += 1

    return BatchResponse(accepted=accepted, errors=errors)
