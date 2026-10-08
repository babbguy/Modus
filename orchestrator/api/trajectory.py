"""
Modus — Trajectory API (Phase 2)
========================================
Endpoints for the Trajectory Engine — session fingerprints, on-demand
simulation, and trajectory decision history.

GET  /api/v1/trajectory/fingerprints   — list session fingerprints
GET  /api/v1/trajectory/simulate       — on-demand trajectory simulation
GET  /api/v1/trajectory/decisions       — list trajectory decisions
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.trajectory_engine import (
    SessionFingerprint as SessionFingerprintStruct,
    linear_extrapolate,
    monte_carlo_simulate,
)
from orchestrator.db.models import (
    SessionFingerprint as SessionFingerprintModel,
    TrajectoryDecision,
)
from orchestrator.db.session import get_read_session

logger = logging.getLogger(__name__)

trajectory_router = APIRouter(prefix="/trajectory", tags=["trajectory"])


# ── Response schemas ──────────────────────────────────────────────────────────


class FingerprintResponse(BaseModel):
    fingerprint_hash: str
    app_id: str
    avg_calls_per_session: float
    std_calls: float
    avg_cost_per_call: float
    std_cost: float
    call_count_p95: float
    branching_factor: float
    sample_count: int
    updated_at: datetime

    class Config:
        from_attributes = True


class SimulationResponse(BaseModel):
    p50_total_cost: float
    p75_total_cost: float
    p95_total_cost: float
    expected_remaining_calls: float
    breach_probability: float
    high_risk_paths: int
    simulation_method: str


class TrajectoryDecisionResponse(BaseModel):
    id: str
    session_id: str
    app_id: str
    team_id: str
    simulation_method: str
    p50_cost: float
    p95_cost: float
    breach_probability: float
    decision: str
    suggested_action: Optional[str] = None
    computed_at: datetime

    class Config:
        from_attributes = True


# ── Fingerprints ──────────────────────────────────────────────────────────────


@trajectory_router.get("/fingerprints", response_model=list[FingerprintResponse])
async def list_fingerprints(
    app_id: Optional[str] = Query(None),
    team_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List session fingerprints with optional filters."""
    q = select(SessionFingerprintModel).order_by(
        SessionFingerprintModel.updated_at.desc()
    )

    if app_id:
        q = q.where(SessionFingerprintModel.app_id == app_id)
    if team_id:
        q = q.where(SessionFingerprintModel.team_id == team_id)
    elif not identity.is_platform_admin:
        q = q.where(SessionFingerprintModel.team_id == identity.team_id)

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


# ── Simulation ────────────────────────────────────────────────────────────────


@trajectory_router.get("/simulate", response_model=SimulationResponse)
async def simulate_trajectory(
    current_spend: float = Query(..., description="Current session spend so far"),
    current_calls: int = Query(..., description="Number of calls made so far"),
    app_id: str = Query(..., description="Application ID"),
    session_id: Optional[str] = Query(None, description="Session ID (informational)"),
    budget: Optional[float] = Query(None, description="Session budget for breach probability"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """
    On-demand trajectory simulation.

    Looks up the most recent fingerprint for the given app_id. If found,
    runs Monte-Carlo simulation; otherwise falls back to linear extrapolation.
    """
    # Look up the best fingerprint for this app
    q = (
        select(SessionFingerprintModel)
        .where(SessionFingerprintModel.app_id == app_id)
        .order_by(SessionFingerprintModel.updated_at.desc())
        .limit(1)
    )
    result = await db.execute(q)
    fp_row = result.scalars().first()

    effective_budget = budget or 0.0

    if fp_row and fp_row.sample_count >= 2:
        # Build the computation struct from the DB model
        fp_struct = SessionFingerprintStruct(
            fingerprint_hash=fp_row.fingerprint_hash,
            avg_calls_per_session=float(fp_row.avg_calls_per_session),
            std_calls=float(fp_row.std_calls),
            avg_cost_per_call=float(fp_row.avg_cost_per_call),
            std_cost=float(fp_row.std_cost),
            call_count_p95=float(fp_row.call_count_p95),
            branching_factor=float(fp_row.branching_factor),
            sample_count=fp_row.sample_count,
        )

        tr = monte_carlo_simulate(
            current_spend=current_spend,
            current_calls=current_calls,
            fingerprint=fp_struct,
            budget=effective_budget,
        )
        method = "monte_carlo"
    else:
        # No fingerprint data — fall back to linear extrapolation with
        # reasonable defaults derived from current session data
        avg_cost = current_spend / max(current_calls, 1)
        avg_calls = float(max(current_calls * 2, 10))  # conservative estimate

        tr = linear_extrapolate(
            current_spend=current_spend,
            current_calls=current_calls,
            avg_cost_per_call=avg_cost,
            avg_calls_per_session=avg_calls,
        )
        method = "linear"

    return SimulationResponse(
        p50_total_cost=tr.p50_total_cost,
        p75_total_cost=tr.p75_total_cost,
        p95_total_cost=tr.p95_total_cost,
        expected_remaining_calls=tr.expected_remaining_calls,
        breach_probability=tr.breach_probability,
        high_risk_paths=tr.high_risk_paths,
        simulation_method=method,
    )


# ── Decisions ─────────────────────────────────────────────────────────────────


@trajectory_router.get("/decisions", response_model=list[TrajectoryDecisionResponse])
async def list_decisions(
    session_id: Optional[str] = Query(None),
    app_id: Optional[str] = Query(None),
    team_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List trajectory decisions with optional filters."""
    q = select(TrajectoryDecision).order_by(
        TrajectoryDecision.computed_at.desc()
    )

    if session_id:
        q = q.where(TrajectoryDecision.session_id == session_id)
    if app_id:
        q = q.where(TrajectoryDecision.app_id == app_id)
    if team_id:
        q = q.where(TrajectoryDecision.team_id == team_id)
    elif not identity.is_platform_admin:
        q = q.where(TrajectoryDecision.team_id == identity.team_id)

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()
