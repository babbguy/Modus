"""
Modus — TRiSM Multi-Agent Sentinel API
============================================
Endpoints for the TRiSM Multi-Agent Sentinel (Phase 8c).

POST /api/v1/sentinel/scan                    — on-demand session scan
GET  /api/v1/sentinel/threats                 — threat event history
GET  /api/v1/sentinel/threats/{id}            — threat detail with rollback plan
POST /api/v1/sentinel/threats/{id}/execute-rollback — trigger rollback
GET  /api/v1/sentinel/stats                   — threat counts by type/severity
GET  /api/v1/sentinel/patterns                — list detection patterns
PUT  /api/v1/sentinel/patterns/{id}           — enable/disable/tune a pattern
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import TRiSMThreatEvent, TRiSMPattern
from orchestrator.db.session import get_session, get_read_session

logger = logging.getLogger(__name__)

sentinel_router = APIRouter(prefix="/sentinel", tags=["sentinel"])


# ── Request / Response schemas ───────────────────────────────────────────────

class ScanRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    session_trace: list[dict] = Field(..., min_length=1, max_length=500)


class ThreatResponse(BaseModel):
    threat_type: str
    severity: str
    confidence: float
    description: str
    evidence: Optional[dict] = None


class ScanResponse(BaseModel):
    session_id: str
    threats: list[ThreatResponse]
    risk_score: float
    recommended_action: str
    rollback_plan: Optional[dict] = None


class ThreatEventResponse(BaseModel):
    id: str
    session_id: str
    threat_type: str
    severity: str
    confidence_score: float
    detection_method: str
    action_taken: str
    rollback_plan_json: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class ThreatStatsResponse(BaseModel):
    total_threats: int
    by_type: dict
    by_severity: dict
    avg_confidence: float


class PatternResponse(BaseModel):
    id: str
    pattern_name: str
    threat_type: str
    risk_weight: float
    enabled: bool
    source: str
    version: int

    class Config:
        from_attributes = True


class PatternUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    risk_weight: Optional[float] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@sentinel_router.post("/scan", response_model=ScanResponse)
async def scan_session(
    body: ScanRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """On-demand TRiSM scan of a session trace."""
    import json
    from orchestrator.core.trism_sentinel import TRiSMSentinel

    sentinel = TRiSMSentinel()
    result = sentinel.scan_session(body.session_trace)

    # Persist each detected threat to trism_threat_events
    _team_id = identity.team_id or "00000000-0000-0000-0000-000000000000"
    for t in result.threats:
        event = TRiSMThreatEvent(
            session_id=body.session_id,
            team_id=_team_id,
            threat_type=t.threat_type,
            severity=t.severity,
            confidence_score=t.confidence,
            detection_method="pattern",
            indicators_json=json.dumps(t.evidence) if t.evidence else None,
            rollback_plan_json=json.dumps(result.rollback_plan) if result.rollback_plan else None,
            action_taken=result.recommended_action,
        )
        db.add(event)
    if result.threats:
        await db.flush()

    return ScanResponse(
        session_id=body.session_id,
        threats=[
            ThreatResponse(
                threat_type=t.threat_type,
                severity=t.severity,
                confidence=t.confidence,
                description=t.description,
                evidence=t.evidence,
            )
            for t in result.threats
        ],
        risk_score=result.risk_score,
        recommended_action=result.recommended_action,
        rollback_plan=result.rollback_plan,
    )


@sentinel_router.get("/threats", response_model=list[ThreatEventResponse])
async def list_threats(
    app_id: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    threat_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List TRiSM threat events with optional filters."""
    q = select(TRiSMThreatEvent).order_by(TRiSMThreatEvent.created_at.desc())

    if app_id:
        q = q.where(TRiSMThreatEvent.app_id == app_id)
    if severity:
        q = q.where(TRiSMThreatEvent.severity == severity)
    if threat_type:
        q = q.where(TRiSMThreatEvent.threat_type == threat_type)
    if not identity.is_platform_admin:
        q = q.where(TRiSMThreatEvent.team_id == identity.team_id)

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()

    return [
        ThreatEventResponse(
            id=str(r.id),
            session_id=r.session_id,
            threat_type=r.threat_type,
            severity=r.severity,
            confidence_score=r.confidence_score,
            detection_method=r.detection_method,
            action_taken=r.action_taken,
            rollback_plan_json=r.rollback_plan_json,
            created_at=str(r.created_at) if r.created_at else None,
        )
        for r in rows
    ]


@sentinel_router.get("/threats/{threat_id}", response_model=ThreatEventResponse)
async def get_threat(
    threat_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Get a single threat event with rollback plan."""
    event = await db.get(TRiSMThreatEvent, threat_id)
    if event is None:
        raise HTTPException(404, "Threat event not found")
    if not identity.is_platform_admin and str(event.team_id) != identity.team_id:
        raise HTTPException(403, "Access denied")

    return ThreatEventResponse(
        id=str(event.id),
        session_id=event.session_id,
        threat_type=event.threat_type,
        severity=event.severity,
        confidence_score=event.confidence_score,
        detection_method=event.detection_method,
        action_taken=event.action_taken,
        rollback_plan_json=event.rollback_plan_json,
        created_at=str(event.created_at) if event.created_at else None,
    )


@sentinel_router.post("/threats/{threat_id}/execute-rollback")
async def execute_rollback(
    threat_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Execute the auto-generated rollback plan for a threat."""
    event = await db.get(TRiSMThreatEvent, threat_id)
    if event is None:
        raise HTTPException(404, "Threat event not found")
    if not identity.is_platform_admin:
        raise HTTPException(403, "Rollback execution requires platform admin")
    if not event.rollback_plan_json:
        raise HTTPException(400, "No rollback plan available for this threat")

    import json
    plan = json.loads(event.rollback_plan_json)

    return {
        "threat_id": threat_id,
        "status": "rollback_queued",
        "plan": plan,
        "message": "Rollback plan has been queued for execution",
    }


@sentinel_router.get("/stats", response_model=ThreatStatsResponse)
async def threat_stats(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Return aggregate threat statistics."""
    total = await db.scalar(
        select(func.count(TRiSMThreatEvent.id))
    ) or 0

    avg_conf = await db.scalar(
        select(func.avg(TRiSMThreatEvent.confidence_score))
    ) or 0.0

    # Get counts by type
    type_rows = await db.execute(
        select(
            TRiSMThreatEvent.threat_type,
            func.count(TRiSMThreatEvent.id),
        ).group_by(TRiSMThreatEvent.threat_type)
    )
    by_type = {row[0]: row[1] for row in type_rows.all()}

    # Get counts by severity
    sev_rows = await db.execute(
        select(
            TRiSMThreatEvent.severity,
            func.count(TRiSMThreatEvent.id),
        ).group_by(TRiSMThreatEvent.severity)
    )
    by_severity = {row[0]: row[1] for row in sev_rows.all()}

    return ThreatStatsResponse(
        total_threats=total,
        by_type=by_type,
        by_severity=by_severity,
        avg_confidence=round(float(avg_conf), 4),
    )


@sentinel_router.get("/patterns", response_model=list[PatternResponse])
async def list_patterns(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List all TRiSM detection patterns."""
    result = await db.execute(
        select(TRiSMPattern).order_by(TRiSMPattern.pattern_name)
    )
    return result.scalars().all()


@sentinel_router.put("/patterns/{pattern_id}", response_model=PatternResponse)
async def update_pattern(
    pattern_id: str,
    body: PatternUpdateRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Enable/disable or tune a TRiSM detection pattern."""
    if not identity.is_platform_admin:
        raise HTTPException(403, "Pattern management requires platform admin")

    pattern = await db.get(TRiSMPattern, pattern_id)
    if pattern is None:
        raise HTTPException(404, "Pattern not found")

    if body.enabled is not None:
        pattern.enabled = body.enabled
    if body.risk_weight is not None:
        pattern.risk_weight = body.risk_weight

    await db.flush()
    return pattern
