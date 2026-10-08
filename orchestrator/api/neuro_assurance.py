"""
Modus — NeuroAI Assurance API (Research Demo)
==================================
REST endpoints for software-simulated neuromorphic assurance metrics and
compliance reports. Energy figures are illustrative constants, not
measurements; there is no hardware energy benefit on standard servers
(see ``orchestrator.core.neuro_assurance``).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.session import get_read_session, get_session

logger = logging.getLogger(__name__)

neuro_assurance_router = APIRouter(prefix="/neuromorphic/assurance", tags=["neuromorphic"])


# ── Response schemas ──────────────────────────────────────────────────────────

class NeuroMetricResponse(BaseModel):
    id: str
    topology_hash: str
    energy_per_spike: float
    stdp_drift: float
    embodied_efficiency_score: float
    spike_rate_hz: float
    active_neuron_ratio: float
    inference_latency_ms: float
    hardware_backend: str
    recorded_at: Optional[str] = None

    class Config:
        from_attributes = True


class NeuroReportResponse(BaseModel):
    report_id: str
    content: str
    metric_count: int


class NeuroBenchResponse(BaseModel):
    topology_hash: str
    latency: dict
    energy: dict
    accuracy: dict
    timestamp: str


class CoEvolutionStatusResponse(BaseModel):
    enabled: bool
    metrics_count_24h: int
    avg_fitness_delta: float
    energy_weight: Optional[float] = None
    drift_weight: Optional[float] = None
    efficiency_weight: Optional[float] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@neuro_assurance_router.get("/metrics", response_model=list[NeuroMetricResponse])
async def list_metrics(
    app_id: Optional[str] = Query(None),
    topology_hash: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> list[NeuroMetricResponse]:
    """List assurance metrics with optional filters."""
    identity.assert_permission("evaluate:read")
    from orchestrator.db.models import NeuroAssuranceMetric

    q = select(NeuroAssuranceMetric).order_by(desc(NeuroAssuranceMetric.recorded_at))

    if app_id:
        q = q.where(NeuroAssuranceMetric.app_id == app_id)
    if topology_hash:
        q = q.where(NeuroAssuranceMetric.topology_hash == topology_hash)
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(NeuroAssuranceMetric.team_id.in_(identity.team_ids))

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    metrics = result.scalars().all()

    return [
        NeuroMetricResponse(
            id=str(m.id),
            topology_hash=m.topology_hash,
            energy_per_spike=m.energy_per_spike,
            stdp_drift=m.stdp_drift,
            embodied_efficiency_score=m.embodied_efficiency_score,
            spike_rate_hz=m.spike_rate_hz,
            active_neuron_ratio=m.active_neuron_ratio,
            inference_latency_ms=m.inference_latency_ms,
            hardware_backend=m.hardware_backend,
            recorded_at=m.recorded_at.isoformat() if m.recorded_at else None,
        )
        for m in metrics
    ]


@neuro_assurance_router.get("/report/{app_id}", response_model=NeuroReportResponse)
async def get_report(
    app_id: str,
    lookback_hours: int = Query(24, ge=1, le=720),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> NeuroReportResponse:
    """Generate or retrieve a compliance report for an app."""
    identity.assert_permission("evaluate:read")

    # Determine team_id from identity
    team_id = identity.team_ids[0] if identity.team_ids else "default"

    from orchestrator.core.neuro_assurance import generate_compliance_report
    result = await generate_compliance_report(db, app_id, team_id, lookback_hours)
    await db.commit()

    if "error" in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=result["error"])

    return NeuroReportResponse(**result)


@neuro_assurance_router.get("/benchmark/{topology_hash}", response_model=NeuroBenchResponse)
async def run_benchmark(
    topology_hash: str,
    identity: Identity = Depends(get_identity),
) -> NeuroBenchResponse:
    """Run NeuroBench suite for a topology."""
    identity.assert_permission("evaluate:read")
    from orchestrator.core.neuro_assurance import NeuroBenchSimulator

    bench = NeuroBenchSimulator(topology_hash, {"neuron_count": 50, "backend": "software"})
    results = bench.run_full_suite()
    return NeuroBenchResponse(**results)


@neuro_assurance_router.get("/co-evolution/status", response_model=CoEvolutionStatusResponse)
async def co_evolution_status(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> CoEvolutionStatusResponse:
    """Co-evolution status and fitness delta."""
    identity.assert_permission("evaluate:read")
    team_id = identity.team_ids[0] if identity.team_ids else "default"

    from orchestrator.core.neuro_assurance import get_co_evolution_status
    result = await get_co_evolution_status(db, team_id)
    return CoEvolutionStatusResponse(**result)
