"""
Modus — Neuromorphic Edge Enforcement API (Research Demo)
===============================================
Endpoints for the Neuromorphic Edge Enforcement Mode (Phase 8e).

This is a research demo backed by a software-simulated SNN. Any power/energy
figures returned are illustrative constants, not measurements, and there is
no hardware energy benefit on standard servers (see
``orchestrator.core.neuro_assurance``).

GET  /api/v1/neuromorphic/status          — enabled, backend, compiled topology count
GET  /api/v1/neuromorphic/metrics         — latency, power (illustrative), spike efficiency
GET  /api/v1/neuromorphic/topology        — compiled SNN topology info
POST /api/v1/neuromorphic/compile         — force recompile SNN for app's policies
GET  /api/v1/neuromorphic/shadow-report   — shadow mode comparison results
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import NeuromorphicMetrics
from orchestrator.db.session import get_read_session

logger = logging.getLogger(__name__)

neuromorphic_router = APIRouter(prefix="/neuromorphic", tags=["neuromorphic"])


# ── Response schemas ─────────────────────────────────────────────────────────

class NeuromorphicStatusResponse(BaseModel):
    enabled: bool
    backend: str
    compiled_topologies: int
    hardware_detected: str


class NeuromorphicMetricsResponse(BaseModel):
    evaluation_count: int
    avg_latency_ms: float
    avg_power_mw: Optional[float] = None
    avg_spike_efficiency: float
    hardware_backend: str


class TopologyResponse(BaseModel):
    topology_hash: str
    neuron_count: int
    synapse_count: int
    input_neurons: int
    output_neurons: int
    policy_types_compiled: list[str]


class CompileRequest(BaseModel):
    policies_yaml: str


class CompileResponse(BaseModel):
    topology_hash: str
    neuron_count: int
    synapse_count: int
    compile_time_ms: float


class ShadowReportResponse(BaseModel):
    total_comparisons: int
    matches: int
    mismatches: int
    mismatch_rate: float
    snn_enabled: bool


# ── Endpoints ────────────────────────────────────────────────────────────────

@neuromorphic_router.get("/status", response_model=NeuromorphicStatusResponse)
async def neuromorphic_status(
    identity: Identity = Depends(get_identity),
):
    """Return neuromorphic enforcement status."""
    from orchestrator.core.config import get_settings
    settings = get_settings()

    compiled = 0
    hw = "none"
    try:
        from orchestrator.core.neuromorphic_engine import NeuromorphicEnforcer
        enforcer = NeuromorphicEnforcer(backend=settings.neuromorphic_backend)
        compiled = len(enforcer._topology_cache)
        from orchestrator.core.neuromorphic_hw import NeuromorphicHardwareDetector
        hw = NeuromorphicHardwareDetector.detect()
    except Exception as exc:
        logger.warning("Neuromorphic status probe failed; reporting defaults: %s", exc)

    return NeuromorphicStatusResponse(
        enabled=settings.neuromorphic_enabled,
        backend=settings.neuromorphic_backend,
        compiled_topologies=compiled,
        hardware_detected=hw,
    )


@neuromorphic_router.get("/metrics", response_model=list[NeuromorphicMetricsResponse])
async def get_metrics(
    app_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Return neuromorphic enforcement metrics."""
    q = select(NeuromorphicMetrics).order_by(
        NeuromorphicMetrics.period_start.desc()
    )

    if app_id:
        q = q.where(NeuromorphicMetrics.app_id == app_id)
    if not identity.is_platform_admin:
        q = q.where(NeuromorphicMetrics.team_id == identity.team_id)

    q = q.limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()

    return [
        NeuromorphicMetricsResponse(
            evaluation_count=r.evaluation_count,
            avg_latency_ms=r.avg_latency_ms,
            avg_power_mw=r.avg_power_mw,
            avg_spike_efficiency=r.spike_efficiency,
            hardware_backend=r.hardware_backend,
        )
        for r in rows
    ]


@neuromorphic_router.get("/topology", response_model=TopologyResponse)
async def get_topology(
    app_id: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
):
    """Return compiled SNN topology info."""
    from orchestrator.core.neuromorphic_engine import NeuromorphicEnforcer
    from orchestrator.core.config import get_settings
    settings = get_settings()

    enforcer = NeuromorphicEnforcer(backend=settings.neuromorphic_backend)

    if not enforcer._topology_cache:
        raise HTTPException(404, "No compiled SNN topology found")

    # Return the first/latest topology
    topo_hash, topology = next(iter(enforcer._topology_cache.items()))

    return TopologyResponse(
        topology_hash=topo_hash,
        neuron_count=topology.neuron_count,
        synapse_count=len(topology.synapses),
        input_neurons=len(topology.input_neurons),
        output_neurons=len(topology.output_neurons),
        policy_types_compiled=[],
    )


@neuromorphic_router.post("/compile", response_model=CompileResponse)
async def compile_policies(
    body: CompileRequest,
    identity: Identity = Depends(get_identity),
):
    """Force recompile SNN for the given policies."""
    import time
    from orchestrator.core.neuromorphic_engine import NeuromorphicEnforcer
    from orchestrator.core.config import get_settings
    settings = get_settings()

    enforcer = NeuromorphicEnforcer(backend=settings.neuromorphic_backend)

    t0 = time.perf_counter()
    topo_hash = enforcer.compile_policies(body.policies_yaml)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    topology = enforcer._topology_cache.get(topo_hash)
    neuron_count = topology.neuron_count if topology else 0
    synapse_count = len(topology.synapses) if topology else 0

    return CompileResponse(
        topology_hash=topo_hash,
        neuron_count=neuron_count,
        synapse_count=synapse_count,
        compile_time_ms=round(elapsed_ms, 2),
    )


@neuromorphic_router.get("/shadow-report", response_model=ShadowReportResponse)
async def shadow_report(
    identity: Identity = Depends(get_identity),
):
    """Return shadow mode comparison results."""
    from orchestrator.core.neuromorphic_engine import NeuromorphicEnforcer
    from orchestrator.core.config import get_settings
    settings = get_settings()

    enforcer = NeuromorphicEnforcer(backend=settings.neuromorphic_backend)

    total = enforcer._shadow_total
    mismatches = enforcer._shadow_mismatches
    matches = total - mismatches
    mismatch_rate = (mismatches / total) if total > 0 else 0.0

    return ShadowReportResponse(
        total_comparisons=total,
        matches=matches,
        mismatches=mismatches,
        mismatch_rate=round(mismatch_rate, 4),
        snn_enabled=mismatch_rate <= 0.01 if total > 0 else True,
    )
