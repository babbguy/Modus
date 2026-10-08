"""
Modus — PQC Assessment API
================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

REST API for PQC readiness assessment and HNDL risk simulation.
Implements features B1 (PQC-Hybrid Crypto-Agility Dashboard) and
B2 (HNDL Risk Simulator) from Phase 10.

Prefix: /api/v1/compliance/pqc-assessment
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.session import get_session, get_read_session

logger = logging.getLogger(__name__)

pqc_assessment_router = APIRouter(
    prefix="/compliance/pqc-assessment",
    tags=["compliance"],
)


# ── Request / Response Models ─────────────────────────────────────────────────

class PQCReadinessResponse(BaseModel):
    team_id: str
    score: float
    classical_count: int
    hybrid_count: int
    pqc_count: int
    weakest_algorithm: Optional[str] = None
    recommendations: list[str] = []
    assessed_at: str


class AppPQCReadinessResponse(BaseModel):
    app_id: str
    team_id: str
    score: float
    classical_count: int
    hybrid_count: int
    pqc_count: int
    weakest_algorithm: Optional[str] = None
    recommendations: list[str] = []
    assessed_at: str


class ReadinessHistoryEntry(BaseModel):
    id: str
    score: float
    classical_key_count: int
    hybrid_key_count: int
    pqc_key_count: int
    weakest_algorithm: Optional[str] = None
    assessed_at: str


class ReadinessHistoryResponse(BaseModel):
    team_id: str
    entries: list[ReadinessHistoryEntry]


class HNDLAssessRequest(BaseModel):
    algorithm: str = Field(..., min_length=1, max_length=64, description="Cryptographic algorithm name")
    key_size: int = Field(..., ge=1, le=100000, description="Key size in bits")
    data_sensitivity: str = Field(
        default="standard",
        description="Data sensitivity: standard | confidential | secret | top_secret",
    )
    team_id: str = Field(..., min_length=1, description="Team ID for audit trail")


class HNDLAssessResponse(BaseModel):
    algorithm: str
    key_size: int
    data_sensitivity: str
    risk_level: str
    estimated_quantum_break_year: int
    recommendation: str
    enforcement_action: str
    years_until_break: int


class HNDLRiskSummaryResponse(BaseModel):
    team_id: str
    total_assessments: int
    risk_counts: dict[str, int]
    most_vulnerable_algorithm: Optional[str] = None
    earliest_break_year: Optional[int] = None


class MigrationLadderResponse(BaseModel):
    team_id: str
    current_stage: int
    stage_name: str
    stage_description: str
    progress_percent: float
    score: float
    stages: list[dict]
    has_assessment: bool


class AgentIdentityResponse(BaseModel):
    app_id: str
    agent_fingerprint: Optional[str] = None
    algorithm: Optional[str] = None
    created_at: Optional[str] = None


class RotateIdentityRequest(BaseModel):
    app_id: str = Field(..., min_length=1, description="App ID to rotate identity for")


class RotateIdentityResponse(BaseModel):
    app_id: str
    new_fingerprint: str
    algorithm: str
    rotated_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pqc_assessment_router.get(
    "/readiness",
    response_model=PQCReadinessResponse,
)
async def get_team_readiness(
    team_id: str = Query(..., min_length=1, description="Team ID"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> PQCReadinessResponse:
    """Assess and return PQC readiness score for a team."""
    from orchestrator.core.pqc_assessment import assess_team_pqc_readiness

    try:
        result = await assess_team_pqc_readiness(db, team_id)
    except Exception as exc:
        logger.error("PQC readiness assessment failed: %s", exc)
        raise HTTPException(status_code=500, detail="Assessment failed")

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return PQCReadinessResponse(**result)


# NOTE: declared before /readiness/{app_id} so "history" is not captured as an app id.
@pqc_assessment_router.get(
    "/readiness/history",
    response_model=ReadinessHistoryResponse,
)
async def get_readiness_history(
    team_id: str = Query(..., min_length=1, description="Team ID"),
    limit: int = Query(default=20, ge=1, le=100, description="Max entries to return"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> ReadinessHistoryResponse:
    """Return historical PQC readiness scores for a team."""
    from orchestrator.db.models import PQCReadinessScore

    try:
        q = (
            select(PQCReadinessScore)
            .where(PQCReadinessScore.team_id == team_id)
            .where(PQCReadinessScore.app_id.is_(None))
            .order_by(desc(PQCReadinessScore.assessed_at))
            .limit(limit)
        )
        rows = (await db.execute(q)).scalars().all()

        entries = [
            ReadinessHistoryEntry(
                id=row.id,
                score=row.score,
                classical_key_count=row.classical_key_count,
                hybrid_key_count=row.hybrid_key_count,
                pqc_key_count=row.pqc_key_count,
                weakest_algorithm=row.weakest_algorithm,
                assessed_at=row.assessed_at.isoformat() if row.assessed_at else "",
            )
            for row in rows
        ]

        return ReadinessHistoryResponse(team_id=team_id, entries=entries)

    except Exception as exc:
        logger.error("Failed to fetch readiness history: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch history")


@pqc_assessment_router.get(
    "/readiness/{app_id}",
    response_model=AppPQCReadinessResponse,
)
async def get_app_readiness(
    app_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> AppPQCReadinessResponse:
    """Assess and return PQC readiness score for a specific app."""
    from orchestrator.core.pqc_assessment import assess_app_pqc_readiness

    if not app_id or not app_id.strip():
        raise HTTPException(status_code=400, detail="app_id is required")

    try:
        result = await assess_app_pqc_readiness(db, app_id)
    except Exception as exc:
        logger.error("App PQC readiness assessment failed: %s", exc)
        raise HTTPException(status_code=500, detail="Assessment failed")

    if "error" in result:
        if result["error"] == "app_not_found":
            raise HTTPException(status_code=404, detail=f"App {app_id} not found")
        raise HTTPException(status_code=400, detail=result["error"])

    return AppPQCReadinessResponse(**result)


@pqc_assessment_router.post(
    "/hndl/assess",
    response_model=HNDLAssessResponse,
)
async def assess_hndl(
    body: HNDLAssessRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> HNDLAssessResponse:
    """Assess HNDL (Harvest-Now-Decrypt-Later) risk for a cryptographic configuration."""
    from orchestrator.core.pqc_assessment import assess_hndl_risk
    from orchestrator.core.config import settings
    from orchestrator.db.models import HNDLRiskAssessment

    # Validate sensitivity
    valid_sensitivities = {"standard", "confidential", "secret", "top_secret"}
    if body.data_sensitivity not in valid_sensitivities:
        raise HTTPException(
            status_code=422,
            detail=f"data_sensitivity must be one of: {', '.join(sorted(valid_sensitivities))}",
        )

    horizon = settings.pqc_hndl_quantum_horizon_years

    result = assess_hndl_risk(
        algorithm=body.algorithm,
        key_size=body.key_size,
        data_sensitivity=body.data_sensitivity,
        horizon_years=horizon,
    )

    # Persist the assessment
    try:
        record = HNDLRiskAssessment(
            team_id=body.team_id,
            algorithm_detected=body.algorithm,
            key_size_bits=body.key_size,
            estimated_quantum_break_year=result["estimated_quantum_break_year"],
            risk_level=result["risk_level"],
            data_sensitivity=body.data_sensitivity,
            recommendation=result["recommendation"],
            enforcement_action=result["enforcement_action"],
        )
        db.add(record)
        await db.flush()
    except Exception as exc:
        logger.error("Failed to persist HNDL assessment: %s", exc)
        # Non-fatal: return the assessment even if persistence fails

    return HNDLAssessResponse(
        algorithm=body.algorithm,
        key_size=body.key_size,
        data_sensitivity=body.data_sensitivity,
        **result,
    )


@pqc_assessment_router.get(
    "/hndl/risk-summary",
    response_model=HNDLRiskSummaryResponse,
)
async def get_hndl_risk_summary(
    team_id: str = Query(..., min_length=1, description="Team ID"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> HNDLRiskSummaryResponse:
    """Return aggregate HNDL risk summary for a team."""
    from sqlalchemy import func
    from orchestrator.db.models import HNDLRiskAssessment

    try:
        # Count by risk level
        risk_q = (
            select(
                HNDLRiskAssessment.risk_level,
                func.count(HNDLRiskAssessment.id),
            )
            .where(HNDLRiskAssessment.team_id == team_id)
            .group_by(HNDLRiskAssessment.risk_level)
        )
        risk_rows = (await db.execute(risk_q)).all()
        risk_counts = {level: count for level, count in risk_rows}
        total = sum(risk_counts.values())

        # Find most vulnerable algorithm
        vuln_q = (
            select(
                HNDLRiskAssessment.algorithm_detected,
                HNDLRiskAssessment.estimated_quantum_break_year,
            )
            .where(HNDLRiskAssessment.team_id == team_id)
            .order_by(HNDLRiskAssessment.estimated_quantum_break_year)
            .limit(1)
        )
        vuln_row = (await db.execute(vuln_q)).first()

        return HNDLRiskSummaryResponse(
            team_id=team_id,
            total_assessments=total,
            risk_counts=risk_counts,
            most_vulnerable_algorithm=vuln_row[0] if vuln_row else None,
            earliest_break_year=vuln_row[1] if vuln_row else None,
        )

    except Exception as exc:
        logger.error("Failed to fetch HNDL risk summary: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch risk summary")


@pqc_assessment_router.get(
    "/migration-ladder",
    response_model=MigrationLadderResponse,
)
async def get_migration_ladder(
    team_id: str = Query(..., min_length=1, description="Team ID"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> MigrationLadderResponse:
    """Return PQC migration ladder status for a team."""
    from orchestrator.core.pqc_assessment import get_migration_ladder_status

    try:
        result = await get_migration_ladder_status(db, team_id)
    except Exception as exc:
        logger.error("Failed to get migration ladder: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to get migration ladder status")

    return MigrationLadderResponse(**{
        k: v for k, v in result.items()
        if k in MigrationLadderResponse.model_fields
    })


@pqc_assessment_router.get(
    "/agent-identity/{app_id}",
    response_model=AgentIdentityResponse,
)
async def get_agent_identity(
    app_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> AgentIdentityResponse:
    """Get PQC agent identity for an app."""
    from orchestrator.db.models import AgentIdentity

    if not app_id or not app_id.strip():
        raise HTTPException(status_code=400, detail="app_id is required")

    try:
        q = (
            select(AgentIdentity)
            .where(AgentIdentity.app_id == app_id)
            .limit(1)
        )
        result = await db.execute(q)
        agent = result.scalar_one_or_none()

        if not agent:
            return AgentIdentityResponse(app_id=app_id)

        return AgentIdentityResponse(
            app_id=app_id,
            agent_fingerprint=agent.agent_fingerprint,
            algorithm=agent.algorithm if hasattr(agent, "algorithm") else None,
            created_at=agent.created_at.isoformat() if hasattr(agent, "created_at") and agent.created_at else None,
        )

    except Exception as exc:
        logger.error("Failed to get agent identity: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to get agent identity")


@pqc_assessment_router.post(
    "/agent-identity/rotate",
    response_model=RotateIdentityResponse,
)
async def rotate_agent_identity(
    body: RotateIdentityRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> RotateIdentityResponse:
    """Rotate the PQC keypair for an agent."""
    from orchestrator.db.models import AgentIdentity, App
    from datetime import datetime, timezone
    import hashlib
    import secrets

    # Verify app exists
    app = await db.get(App, body.app_id)
    if not app:
        raise HTTPException(status_code=404, detail=f"App {body.app_id} not found")

    try:
        # Generate new fingerprint
        entropy = secrets.token_bytes(32)
        new_fingerprint = hashlib.sha256(entropy).hexdigest()
        now = datetime.now(timezone.utc)

        # Check for existing identity
        q = select(AgentIdentity).where(AgentIdentity.app_id == body.app_id).limit(1)
        result = await db.execute(q)
        existing = result.scalar_one_or_none()

        if existing:
            existing.agent_fingerprint = new_fingerprint
            if hasattr(existing, "rotated_at"):
                existing.rotated_at = now
        else:
            new_identity = AgentIdentity(
                app_id=body.app_id,
                agent_fingerprint=new_fingerprint,
            )
            db.add(new_identity)

        await db.flush()

        return RotateIdentityResponse(
            app_id=body.app_id,
            new_fingerprint=new_fingerprint,
            algorithm="ML-KEM-768-shim",
            rotated_at=now.isoformat(),
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to rotate agent identity: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to rotate agent identity")
