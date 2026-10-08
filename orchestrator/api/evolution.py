"""
Modus — Constitutional Evolution API
==========================================
Endpoints for the Constitutional Evolution Engine (Phase 8d).

GET  /api/v1/governance/evolution/status      — current evolution status
GET  /api/v1/governance/evolution/generations  — generation history
GET  /api/v1/governance/evolution/proposals    — evolved policy proposals
POST /api/v1/governance/evolution/proposals/{id}/accept  — apply evolved policy
POST /api/v1/governance/evolution/proposals/{id}/reject  — reject with reason
POST /api/v1/governance/evolution/trigger      — manually trigger evolution
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity, team_scope_clause
from orchestrator.db.models import EvolutionGeneration, EvolutionProposal
from orchestrator.db.session import get_session, get_read_session

logger = logging.getLogger(__name__)

evolution_router = APIRouter(prefix="/governance/evolution", tags=["governance"])


# ── Response schemas ─────────────────────────────────────────────────────────

class EvolutionStatusResponse(BaseModel):
    enabled: bool
    latest_generation: Optional[int] = None
    latest_best_fitness: Optional[float] = None
    latest_population_size: Optional[int] = None
    total_generations: int
    total_mutations: int = 0
    pending_proposals: int


class GenerationResponse(BaseModel):
    id: str
    generation_number: int
    population_size: int
    best_fitness: float
    avg_fitness: float
    best_genome_yaml: str
    elapsed_ms: int
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class EvolutionProposalResponse(BaseModel):
    id: str
    generation_id: str
    fitness_score: float
    constitutional_diff: str
    rationale: Optional[str] = None
    status: str
    proposal_yaml: str
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1)


def _count_mutations(raw) -> int:
    """Number of mutations recorded on one generation row.

    ``mutations_applied`` holds a JSON list of human-readable mutation
    descriptions (see constitutional_engine._describe_mutations); a JSON object
    with a ``count`` key is also accepted. Anything unparseable counts as zero.
    """
    if not raw:
        return 0
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        try:
            return int(data.get("count", 0))
        except (TypeError, ValueError):
            return 0
    return 0


# ── Endpoints ────────────────────────────────────────────────────────────────

@evolution_router.get("/status", response_model=EvolutionStatusResponse)
async def evolution_status(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Return current evolution engine status."""
    from orchestrator.core.config import get_settings
    settings = get_settings()

    teams = identity.visible_team_ids()
    gen_scope = team_scope_clause(EvolutionGeneration.team_id, teams)
    prop_scope = team_scope_clause(EvolutionProposal.team_id, teams)

    total_gens = await db.scalar(
        select(func.count(EvolutionGeneration.id)).where(gen_scope)
    ) or 0

    pending = await db.scalar(
        select(func.count(EvolutionProposal.id))
        .where(EvolutionProposal.status == "pending", prop_scope)
    ) or 0

    latest_gen = None
    latest_fitness = None
    latest_population = None
    total_mutations = 0
    if total_gens > 0:
        result = await db.execute(
            select(EvolutionGeneration)
            .where(gen_scope)
            .order_by(EvolutionGeneration.created_at.desc())
            .limit(1)
        )
        latest = result.scalars().first()
        if latest:
            latest_gen = latest.generation_number
            latest_fitness = latest.best_fitness
            latest_population = latest.population_size
        raw = await db.execute(
            select(EvolutionGeneration.mutations_applied).where(gen_scope)
        )
        total_mutations = sum(_count_mutations(m) for (m,) in raw.all())

    return EvolutionStatusResponse(
        enabled=settings.evolution_enabled,
        latest_generation=latest_gen,
        latest_best_fitness=latest_fitness,
        latest_population_size=latest_population,
        total_generations=total_gens,
        total_mutations=total_mutations,
        pending_proposals=pending,
    )


@evolution_router.get("/generations", response_model=list[GenerationResponse])
async def list_generations(
    team_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List evolution generations with fitness history."""
    q = select(EvolutionGeneration).order_by(
        EvolutionGeneration.created_at.desc()
    )

    if team_id:
        q = q.where(EvolutionGeneration.team_id == team_id)
    else:
        q = q.where(team_scope_clause(EvolutionGeneration.team_id, identity.visible_team_ids()))

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()

    return [
        GenerationResponse(
            id=str(r.id),
            generation_number=r.generation_number,
            population_size=r.population_size,
            best_fitness=r.best_fitness,
            avg_fitness=r.avg_fitness,
            best_genome_yaml=r.best_genome_yaml,
            elapsed_ms=r.elapsed_ms,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


@evolution_router.get("/proposals", response_model=list[EvolutionProposalResponse])
async def list_proposals(
    team_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List evolved policy proposals."""
    q = select(EvolutionProposal).order_by(
        EvolutionProposal.created_at.desc()
    )

    if team_id:
        q = q.where(EvolutionProposal.team_id == team_id)
    else:
        q = q.where(team_scope_clause(EvolutionProposal.team_id, identity.visible_team_ids()))
    if status:
        q = q.where(EvolutionProposal.status == status)

    q = q.limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()

    return [
        EvolutionProposalResponse(
            id=str(r.id),
            generation_id=str(r.generation_id),
            fitness_score=r.fitness_score,
            constitutional_diff=r.constitutional_diff,
            rationale=r.rationale,
            status=r.status,
            proposal_yaml=r.proposal_yaml,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


@evolution_router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(
    proposal_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Accept an evolved policy proposal — creates GovernancePolicy."""
    proposal = await db.get(EvolutionProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Evolution proposal not found")
    if proposal.status != "pending":
        raise HTTPException(400, f"Proposal is {proposal.status}, not pending")

    proposal.status = "accepted"
    proposal.accepted_by = identity.user_id or "admin"
    await db.flush()

    return {
        "proposal_id": proposal_id,
        "status": "accepted",
        "message": "Evolved policy proposal accepted",
    }


@evolution_router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    body: RejectRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Reject an evolved policy proposal with reason."""
    proposal = await db.get(EvolutionProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Evolution proposal not found")
    if proposal.status != "pending":
        raise HTTPException(400, f"Proposal is {proposal.status}, not pending")

    proposal.status = "rejected"
    await db.flush()

    return {
        "proposal_id": proposal_id,
        "status": "rejected",
        "reason": body.reason,
    }


@evolution_router.post("/trigger")
async def trigger_evolution(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Manually trigger an evolution run (admin only)."""
    if not identity.is_platform_admin:
        raise HTTPException(403, "Evolution trigger requires platform admin")

    from orchestrator.core.constitutional_engine import run_evolution_loop
    await run_evolution_loop(db)

    return {"status": "evolution_triggered", "message": "Evolution run completed"}
