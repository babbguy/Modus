"""
Modus — Governance API
================================
Endpoints for the Autonomous Governance Loop.

GET  /api/v1/governance/proposals          — list governance proposals
GET  /api/v1/governance/proposals/{id}     — single proposal detail
POST /api/v1/governance/proposals/{id}/apply   — apply proposal (create policy)
POST /api/v1/governance/proposals/{id}/dismiss — dismiss with reason
GET  /api/v1/governance/stats              — summary statistics
POST /api/v1/governance/rewind-event       — SDK posts failure context
GET  /api/v1/governance/rewind-events      — rewind event history
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.governance_loop import apply_proposal, dismiss_proposal
from orchestrator.db.models import GovernanceProposal, RewindEvent
from orchestrator.db.session import get_session, get_read_session

logger = logging.getLogger(__name__)

governance_router = APIRouter(prefix="/governance", tags=["governance"])


# ── Response schemas ──────────────────────────────────────────────────────────

class ProposalResponse(BaseModel):
    id: str
    team_id: str
    app_id: Optional[str] = None
    proposal_type: str
    severity: str
    title: str
    rationale: str
    current_yaml: Optional[str] = None
    proposed_yaml: str
    data_snapshot: Optional[dict] = None
    estimated_savings_usd: Optional[float] = None
    source: str
    status: str
    created_at: datetime
    applied_at: Optional[datetime] = None
    applied_by: Optional[str] = None
    dismissed_at: Optional[datetime] = None
    dismissed_reason: Optional[str] = None
    expires_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProposalStatsResponse(BaseModel):
    pending: int = 0
    applied: int = 0
    dismissed: int = 0
    expired: int = 0
    total_estimated_savings: float = 0.0


class DismissRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class RewindEventRequest(BaseModel):
    session_id: str
    app_id: str
    team_id: str
    trigger_reason: str
    actions_rolled_back: Optional[list] = None
    failure_context: Optional[dict] = None


class RewindEventResponse(BaseModel):
    id: str
    session_id: str
    app_id: str
    team_id: str
    trigger_reason: str
    actions_rolled_back: Optional[list] = None
    failure_context: Optional[dict] = None
    policy_diff_generated: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── Proposals ─────────────────────────────────────────────────────────────────

@governance_router.get("/proposals", response_model=list[ProposalResponse])
async def list_proposals(
    team_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List governance proposals with optional filters."""
    q = select(GovernanceProposal).order_by(
        GovernanceProposal.created_at.desc()
    )

    if team_id:
        q = q.where(GovernanceProposal.team_id == team_id)
    elif not identity.is_platform_admin:
        q = q.where(GovernanceProposal.team_id == identity.team_id)

    if status:
        q = q.where(GovernanceProposal.status == status)

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


@governance_router.get("/proposals/{proposal_id}", response_model=ProposalResponse)
async def get_proposal(
    proposal_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Get a single governance proposal."""
    proposal = await db.get(GovernanceProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Proposal not found")
    if not identity.is_platform_admin and str(proposal.team_id) != identity.team_id:
        raise HTTPException(403, "Access denied")
    return proposal


@governance_router.post("/proposals/{proposal_id}/apply")
async def apply_proposal_endpoint(
    proposal_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Apply a governance proposal — creates a new GovernancePolicy."""
    proposal = await db.get(GovernanceProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Proposal not found")
    if proposal.status != "pending":
        raise HTTPException(400, f"Proposal status is '{proposal.status}', not 'pending'")
    if not identity.is_platform_admin and str(proposal.team_id) != identity.team_id:
        raise HTTPException(403, "Access denied")

    policy = await apply_proposal(db, proposal_id, identity.user_id or "system")
    if policy is None:
        raise HTTPException(400, "Failed to apply proposal")

    await db.commit()
    return {
        "applied": True,
        "policy_id": policy.id,
        "policy_name": policy.name,
        "proposal_id": proposal_id,
    }


@governance_router.post("/proposals/{proposal_id}/dismiss")
async def dismiss_proposal_endpoint(
    proposal_id: str,
    body: DismissRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Dismiss a governance proposal with reason."""
    proposal = await db.get(GovernanceProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "Proposal not found")
    if not identity.is_platform_admin and str(proposal.team_id) != identity.team_id:
        raise HTTPException(403, "Access denied")

    success = await dismiss_proposal(db, proposal_id, body.reason)
    if not success:
        raise HTTPException(400, "Failed to dismiss proposal")

    await db.commit()
    return {"dismissed": True, "proposal_id": proposal_id}


@governance_router.get("/stats", response_model=ProposalStatsResponse)
async def get_governance_stats(
    team_id: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Summary statistics for governance proposals."""
    base = select(
        GovernanceProposal.status,
        func.count().label("count"),
        func.coalesce(func.sum(GovernanceProposal.estimated_savings_usd), 0).label("savings"),
    )

    if team_id:
        base = base.where(GovernanceProposal.team_id == team_id)
    elif not identity.is_platform_admin:
        base = base.where(GovernanceProposal.team_id == identity.team_id)

    base = base.group_by(GovernanceProposal.status)
    result = await db.execute(base)

    stats = ProposalStatsResponse()
    for row in result.all():
        count = row.count or 0
        savings = float(row.savings or 0)
        if row.status == "pending":
            stats.pending = count
            stats.total_estimated_savings = savings
        elif row.status == "applied":
            stats.applied = count
        elif row.status == "dismissed":
            stats.dismissed = count
        elif row.status == "expired":
            stats.expired = count

    return stats


# ── Rewind Events ─────────────────────────────────────────────────────────────

@governance_router.post("/rewind-event", status_code=201)
async def create_rewind_event(
    body: RewindEventRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """
    SDK posts failure context for experience distillation.
    Called by the SDK after a rewind event triggers.
    """
    event = RewindEvent(
        session_id=body.session_id,
        app_id=body.app_id,
        team_id=body.team_id,
        trigger_reason=body.trigger_reason,
        actions_rolled_back=body.actions_rolled_back,
        failure_context=body.failure_context,
    )
    db.add(event)
    await db.commit()
    return {"id": event.id, "created": True}


@governance_router.get("/rewind-events", response_model=list[RewindEventResponse])
async def list_rewind_events(
    app_id: Optional[str] = Query(None),
    team_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List rewind events with optional filters."""
    q = select(RewindEvent).order_by(RewindEvent.created_at.desc())

    if app_id:
        q = q.where(RewindEvent.app_id == app_id)
    if team_id:
        q = q.where(RewindEvent.team_id == team_id)
    elif not identity.is_platform_admin:
        q = q.where(RewindEvent.team_id == identity.team_id)

    q = q.limit(limit)
    result = await db.execute(q)
    return result.scalars().all()
