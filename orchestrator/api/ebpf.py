"""
Modus — eBPF Advisory Budget Tracking API (Preview, Phase 11a)
=========================================================
Endpoints for managing the eBPF advisory budget-tracking engine.

NOTE: By default this is advisory, in-process budget tracking (Preview) —
it observes spend but does not drop packets. Kernel-level eBPF enforcement
is experimental, Linux-only (kernel >= 5.10 + root + bcc/libbpf), not active
by default, and not production-viable as written (see
``orchestrator.core.ebpf_engine``).

GET  /api/v1/ebpf/status          — engine status, backend, uptime
GET  /api/v1/ebpf/stats           — interception statistics
GET  /api/v1/ebpf/events          — recent intercept events
POST /api/v1/ebpf/start           — start the eBPF engine
POST /api/v1/ebpf/stop            — stop the eBPF engine
POST /api/v1/ebpf/budget          — set per-app budget in the BPF map
GET  /api/v1/ebpf/budget/{app_id} — get current budget state
GET  /api/v1/ebpf/providers       — list tracked AI provider endpoints
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from orchestrator.core.auth import Identity, get_identity

logger = logging.getLogger(__name__)

ebpf_router = APIRouter(prefix="/ebpf", tags=["ebpf"])


# ── Response schemas ─────────────────────────────────────────────────────────

class EBPFStatusResponse(BaseModel):
    backend: str
    is_active: bool
    uptime_seconds: float
    kernel_version: str
    bpf_program_loaded: bool


class EBPFStatsResponse(BaseModel):
    backend: str
    is_active: bool
    uptime_seconds: float
    total_intercepted: int
    total_allowed: int
    total_dropped: int
    total_redirected: int
    avg_latency_us: float
    tracked_apps: int
    tracked_providers: int


class InterceptEventResponse(BaseModel):
    timestamp_ns: int
    pid: int
    comm: str
    provider: str
    destination_ip: str
    destination_port: int
    model_hint: str
    estimated_tokens: int
    action_taken: str
    latency_us: float


class SetBudgetRequest(BaseModel):
    app_id: str = Field(..., min_length=1, max_length=256)
    budget_limit_cents: int = Field(..., gt=0, le=100_000_000)
    reset_interval_seconds: int = Field(default=3600, ge=0, le=86400 * 365)
    action_on_exceed: str = Field(default="drop", pattern=r"^(drop|allow|redirect|log)$")


class BudgetResponse(BaseModel):
    app_id: str
    budget_limit_cents: int
    spent_cents: int
    call_count: int
    remaining_cents: int
    action_on_exceed: str


class StartRequest(BaseModel):
    interface: str = "eth0"


class ProviderEntry(BaseModel):
    provider: str
    hosts: list[str]


# ── Endpoints ────────────────────────────────────────────────────────────────

@ebpf_router.get("/status", response_model=EBPFStatusResponse)
async def ebpf_status(identity: Identity = Depends(get_identity)):
    """Get eBPF engine status."""
    from orchestrator.core.ebpf_engine import get_ebpf_engine
    engine = get_ebpf_engine()
    stats = engine.get_stats()
    return EBPFStatusResponse(
        backend=stats.backend,
        is_active=stats.is_active,
        uptime_seconds=stats.uptime_seconds,
        kernel_version=stats.kernel_version,
        bpf_program_loaded=stats.bpf_program_loaded,
    )


@ebpf_router.get("/stats", response_model=EBPFStatsResponse)
async def ebpf_stats(identity: Identity = Depends(get_identity)):
    """Get eBPF interception statistics."""
    from orchestrator.core.ebpf_engine import get_ebpf_engine
    engine = get_ebpf_engine()
    stats = engine.get_stats()
    return EBPFStatsResponse(
        backend=stats.backend,
        is_active=stats.is_active,
        uptime_seconds=stats.uptime_seconds,
        total_intercepted=stats.total_intercepted,
        total_allowed=stats.total_allowed,
        total_dropped=stats.total_dropped,
        total_redirected=stats.total_redirected,
        avg_latency_us=stats.avg_latency_us,
        tracked_apps=stats.tracked_apps,
        tracked_providers=stats.tracked_providers,
    )


@ebpf_router.get("/events", response_model=list[InterceptEventResponse])
async def ebpf_events(
    limit: int = Query(default=100, ge=1, le=1000),
    identity: Identity = Depends(get_identity),
):
    """Get recent intercept events."""
    from orchestrator.core.ebpf_engine import get_ebpf_engine
    engine = get_ebpf_engine()
    events = engine.get_recent_events(limit)
    return [
        InterceptEventResponse(
            timestamp_ns=e.timestamp_ns,
            pid=e.pid,
            comm=e.comm,
            provider=e.provider,
            destination_ip=e.destination_ip,
            destination_port=e.destination_port,
            model_hint=e.model_hint,
            estimated_tokens=e.estimated_tokens,
            action_taken=e.action_taken,
            latency_us=e.latency_us,
        )
        for e in events
    ]


@ebpf_router.post("/start")
async def ebpf_start(
    req: StartRequest = StartRequest(),
    identity: Identity = Depends(get_identity),
):
    """Start the eBPF enforcement engine."""
    from orchestrator.core.ebpf_engine import get_ebpf_engine
    engine = get_ebpf_engine()
    if engine.is_active:
        return {"status": "already_running", "backend": engine.backend.value}
    used_ebpf = engine.start(interface=req.interface)
    return {
        "status": "started",
        "backend": engine.backend.value,
        "using_ebpf": used_ebpf,
    }


@ebpf_router.post("/stop")
async def ebpf_stop(identity: Identity = Depends(get_identity)):
    """Stop the eBPF enforcement engine."""
    from orchestrator.core.ebpf_engine import get_ebpf_engine
    engine = get_ebpf_engine()
    engine.stop()
    return {"status": "stopped"}


@ebpf_router.post("/budget")
async def ebpf_set_budget(
    req: SetBudgetRequest,
    identity: Identity = Depends(get_identity),
):
    """Set or update a per-app budget in the eBPF enforcement map."""
    from orchestrator.core.ebpf_engine import (
        get_ebpf_engine, AppBudget, EnforcementAction,
    )
    engine = get_ebpf_engine()

    action_map = {
        "drop": EnforcementAction.DROP,
        "allow": EnforcementAction.ALLOW,
        "redirect": EnforcementAction.REDIRECT,
        "log": EnforcementAction.LOG,
    }

    budget = AppBudget(
        app_id=req.app_id,
        budget_limit_cents=req.budget_limit_cents,
        reset_interval_seconds=req.reset_interval_seconds,
        action_on_exceed=action_map.get(req.action_on_exceed, EnforcementAction.DROP),
    )
    engine.set_budget(req.app_id, budget)
    return {"status": "ok", "app_id": req.app_id, "budget_limit_cents": req.budget_limit_cents}


@ebpf_router.get("/budget/{app_id}", response_model=Optional[BudgetResponse])
async def ebpf_get_budget(
    app_id: str,
    identity: Identity = Depends(get_identity),
):
    """Get current budget state for an application."""
    from orchestrator.core.ebpf_engine import get_ebpf_engine
    engine = get_ebpf_engine()
    budget = engine.get_budget(app_id)
    if budget is None:
        raise HTTPException(404, f"No budget found for app {app_id}")
    return BudgetResponse(
        app_id=budget.app_id,
        budget_limit_cents=budget.budget_limit_cents,
        spent_cents=budget.spent_cents,
        call_count=budget.call_count,
        remaining_cents=max(0, budget.budget_limit_cents - budget.spent_cents),
        action_on_exceed=budget.action_on_exceed.value,
    )


@ebpf_router.get("/providers", response_model=list[ProviderEntry])
async def ebpf_providers(identity: Identity = Depends(get_identity)):
    """List tracked AI provider endpoints."""
    from orchestrator.core.ebpf_engine import AI_PROVIDER_ENDPOINTS
    return [
        ProviderEntry(provider=provider, hosts=hosts)
        for provider, hosts in AI_PROVIDER_ENDPOINTS.items()
    ]
