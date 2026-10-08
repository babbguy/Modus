"""
Modus — CRDT Advisory Local Cache API (Preview, Phase 11b)
==============================================================
Endpoints for managing the CRDT advisory local cache / sync hub.

NOTE: The CRDT layer is an advisory local cache (Preview) that converges to
the database of record; it is not a hard-cap budget enforcer under network
partition, and state persists to a JSON side-file (SQLite integration is
unfinished). See ``orchestrator.core.crdt_sync``.

GET  /api/v1/crdt/status              — hub status, node count, tracked apps
GET  /api/v1/crdt/stats               — gossip/merge statistics
GET  /api/v1/crdt/state               — all app CRDT states (merged global view)
GET  /api/v1/crdt/state/{app_id}      — CRDT state for specific app
POST /api/v1/crdt/budget              — set budget limit for an app
POST /api/v1/crdt/spend               — record a spend (for testing / manual)
GET  /api/v1/crdt/check/{app_id}      — check if app is within budget
POST /api/v1/crdt/start               — start the CRDT hub
POST /api/v1/crdt/stop                — stop the CRDT hub
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from orchestrator.core.auth import Identity, get_identity

logger = logging.getLogger(__name__)

crdt_router = APIRouter(prefix="/crdt", tags=["crdt"])


# ── Response schemas ─────────────────────────────────────────────────────────

class CRDTStatusResponse(BaseModel):
    active: bool
    listen_port: int
    known_nodes: int
    tracked_apps: int


class CRDTStatsResponse(BaseModel):
    active: bool
    listen_port: int
    known_nodes: int
    tracked_apps: int
    deltas_received: int
    merges_performed: int
    broadcasts_sent: int
    persist_cycles: int


class AppStateResponse(BaseModel):
    app_id: str
    spend_cents: int
    call_count: int
    budget_limit_cents: Optional[int] = None
    remaining_cents: int
    is_over_budget: bool


class BudgetCheckResponse(BaseModel):
    app_id: str
    allowed: bool
    remaining_cents: int


class SetBudgetRequest(BaseModel):
    app_id: str = Field(..., min_length=1, max_length=256)
    limit_cents: int = Field(..., gt=0, le=100_000_000)  # max $1M


class RecordSpendRequest(BaseModel):
    app_id: str = Field(..., min_length=1, max_length=256)
    cost_cents: int = Field(..., gt=0, le=10_000_000)  # max $100K per call


class StartRequest(BaseModel):
    listen_port: int = 9471
    persist_interval_seconds: float = 10.0


# ── Endpoints ────────────────────────────────────────────────────────────────

@crdt_router.get("/status", response_model=CRDTStatusResponse)
async def crdt_status(identity: Identity = Depends(get_identity)):
    """Get CRDT hub status."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    stats = hub.get_stats()
    return CRDTStatusResponse(
        active=stats["active"],
        listen_port=stats["listen_port"],
        known_nodes=stats["known_nodes"],
        tracked_apps=stats["tracked_apps"],
    )


@crdt_router.get("/stats", response_model=CRDTStatsResponse)
async def crdt_stats(identity: Identity = Depends(get_identity)):
    """Get CRDT gossip and merge statistics."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    stats = hub.get_stats()
    return CRDTStatsResponse(**stats)


@crdt_router.get("/state", response_model=list[AppStateResponse])
async def crdt_all_states(identity: Identity = Depends(get_identity)):
    """Get all merged app CRDT states."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    states = hub.get_global_state()
    results = []
    for app_id, state in states.items():
        limit = state.budget_limit_cents.value
        results.append(AppStateResponse(
            app_id=app_id,
            spend_cents=state.spend_cents.value,
            call_count=state.call_count.value,
            budget_limit_cents=limit if limit else None,
            remaining_cents=state.remaining_cents(),
            is_over_budget=state.is_over_budget(),
        ))
    return results


@crdt_router.get("/state/{app_id}", response_model=AppStateResponse)
async def crdt_app_state(
    app_id: str,
    identity: Identity = Depends(get_identity),
):
    """Get CRDT state for a specific application."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    state = hub.get_app_state(app_id)
    if state is None:
        raise HTTPException(404, f"No CRDT state for app {app_id}")
    limit = state.budget_limit_cents.value
    return AppStateResponse(
        app_id=app_id,
        spend_cents=state.spend_cents.value,
        call_count=state.call_count.value,
        budget_limit_cents=limit if limit else None,
        remaining_cents=state.remaining_cents(),
        is_over_budget=state.is_over_budget(),
    )


@crdt_router.post("/budget")
async def crdt_set_budget(
    req: SetBudgetRequest,
    identity: Identity = Depends(get_identity),
):
    """Set the budget limit for an app via CRDT."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    hub.set_budget(req.app_id, req.limit_cents)
    return {"status": "ok", "app_id": req.app_id, "limit_cents": req.limit_cents}


@crdt_router.post("/spend")
async def crdt_record_spend(
    req: RecordSpendRequest,
    identity: Identity = Depends(get_identity),
):
    """Record a spend for an app (testing / manual override)."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    new_total = hub.record_spend(req.app_id, req.cost_cents)
    return {"status": "ok", "app_id": req.app_id, "new_total_cents": new_total}


@crdt_router.get("/check/{app_id}", response_model=BudgetCheckResponse)
async def crdt_check_budget(
    app_id: str,
    identity: Identity = Depends(get_identity),
):
    """Check if an app is within budget."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    state = hub.get_app_state(app_id)
    if state is None:
        return BudgetCheckResponse(app_id=app_id, allowed=True, remaining_cents=999999999)
    return BudgetCheckResponse(
        app_id=app_id,
        allowed=not state.is_over_budget(),
        remaining_cents=state.remaining_cents(),
    )


@crdt_router.post("/start")
async def crdt_start(
    req: StartRequest = StartRequest(),
    identity: Identity = Depends(get_identity),
):
    """Start the CRDT hub."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    if hub.is_active:
        return {"status": "already_running"}
    hub.listen_port = req.listen_port
    hub.persist_interval = req.persist_interval_seconds
    hub.start()
    return {"status": "started", "listen_port": req.listen_port}


@crdt_router.post("/stop")
async def crdt_stop(identity: Identity = Depends(get_identity)):
    """Stop the CRDT hub."""
    from orchestrator.core.crdt_sync import get_crdt_hub
    hub = get_crdt_hub()
    hub.stop()
    return {"status": "stopped"}
