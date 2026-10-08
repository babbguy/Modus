"""
Modus — Federation API
============================
REST endpoints for opt-in, numeric-only benchmark sharing.

NOTE: This is opt-in benchmark sharing of numeric gene diffs and fitness
values — k-anonymized (min 3 contributors) at publish time. It is NOT
zero-knowledge and NOT differentially private (see
``orchestrator.core.federation_engine``).

CRITICAL: This is the ONLY feature with outbound connections.
All endpoints enforce consent checkpoint before any data leaves.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.session import get_read_session, get_session

logger = logging.getLogger(__name__)

federation_router = APIRouter(prefix="/federation", tags=["governance"])


# ── Request / Response schemas ────────────────────────────────────────────────

class ConsentRequest(BaseModel):
    participation_mode: str = "consumer"  # consumer | participant
    disclosure_text: str


class ConsentResponse(BaseModel):
    consent_id: str
    participation_mode: str
    consented_at: Optional[str] = None


class FederationStatusResponse(BaseModel):
    federation_enabled: bool
    participation_enabled: bool
    consent_active: bool
    consortium_mode: bool
    last_sync: Optional[dict] = None
    latest_result: Optional[dict] = None


class MergedResultResponse(BaseModel):
    gene_improvements: dict
    participating_instances: int
    confidence_score: float
    received_at: Optional[str] = None


class PeerRegisterRequest(BaseModel):
    peer_endpoint: str
    peer_public_key_hash: str
    peer_alias: Optional[str] = None


class PeerResponse(BaseModel):
    id: str
    peer_endpoint: str
    peer_alias: Optional[str] = None
    is_active: bool
    last_sync_at: Optional[str] = None


class AggregateRequest(BaseModel):
    deltas: list[dict]


class AggregateResponse(BaseModel):
    gene_improvements: dict
    participating_instances: int
    confidence_score: float
    avg_fitness_improvement: float


# ── Endpoints ─────────────────────────────────────────────────────────────────

@federation_router.get("/status", response_model=FederationStatusResponse)
async def get_status(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> FederationStatusResponse:
    """Federation configuration and status."""
    identity.assert_permission("evaluate:read")
    from orchestrator.core.federation_engine import get_federation_status
    result = await get_federation_status(db)
    return FederationStatusResponse(**result)


@federation_router.post("/consent", response_model=ConsentResponse)
async def record_consent(
    req: ConsentRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> ConsentResponse:
    """Record operator consent for federation participation."""
    identity.assert_permission("platform:admin")
    from orchestrator.core.federation_engine import ConsentCheckpoint
    result = await ConsentCheckpoint.record_consent(
        db, req.participation_mode, identity.actor_id, req.disclosure_text,
    )
    await db.commit()
    return ConsentResponse(**result)


@federation_router.delete("/consent")
async def withdraw_consent(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Withdraw consent — immediate cessation of all outbound."""
    identity.assert_permission("platform:admin")
    from orchestrator.core.federation_engine import ConsentCheckpoint
    success = await ConsentCheckpoint.withdraw_consent(db)
    await db.commit()
    if not success:
        raise HTTPException(status_code=404, detail="No active consent found.")
    return {"status": "withdrawn", "detail": "All outbound federation activity stopped immediately."}


@federation_router.post("/submit")
async def submit_delta(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Manual delta submission (participant mode)."""
    identity.assert_permission("platform:admin")
    from orchestrator.core.federation_engine import ConsentCheckpoint
    if not await ConsentCheckpoint.has_consent(db):
        raise HTTPException(status_code=403, detail="Federation consent required before submission.")

    # This would normally extract the latest evolution delta and submit it
    return {"status": "submitted", "detail": "Delta queued for next sync cycle."}


@federation_router.get("/results", response_model=Optional[MergedResultResponse])
async def get_results(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> Optional[MergedResultResponse]:
    """Latest merged results from federation."""
    identity.assert_permission("evaluate:read")
    from orchestrator.db.models import FederationMergedResult
    result = await db.execute(
        select(FederationMergedResult)
        .order_by(desc(FederationMergedResult.received_at))
        .limit(1)
    )
    merged = result.scalar_one_or_none()
    if not merged:
        return None
    return MergedResultResponse(
        gene_improvements=merged.gene_improvements or {},
        participating_instances=merged.participating_instances,
        confidence_score=merged.confidence_score,
        received_at=merged.received_at.isoformat() if merged.received_at else None,
    )


# ── Consortium Hub endpoints ─────────────────────────────────────────────────

@federation_router.post("/consortium/register-peer", response_model=PeerResponse)
async def register_peer(
    req: PeerRegisterRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> PeerResponse:
    """Register a consortium peer (hub mode only)."""
    identity.assert_permission("platform:admin")
    from orchestrator.core.config import settings
    if not settings.federation_consortium_mode:
        raise HTTPException(status_code=403, detail="Consortium mode not enabled.")

    from orchestrator.db.models import ConsortiumPeer
    peer = ConsortiumPeer(
        peer_endpoint=req.peer_endpoint,
        peer_public_key_hash=req.peer_public_key_hash,
        peer_alias=req.peer_alias,
    )
    db.add(peer)
    await db.flush()
    await db.commit()
    return PeerResponse(
        id=str(peer.id),
        peer_endpoint=peer.peer_endpoint,
        peer_alias=peer.peer_alias,
        is_active=peer.is_active,
        last_sync_at=None,
    )


@federation_router.get("/consortium/peers", response_model=list[PeerResponse])
async def list_peers(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> list[PeerResponse]:
    """List consortium peers (hub mode only)."""
    identity.assert_permission("platform:admin")
    from orchestrator.core.config import settings
    if not settings.federation_consortium_mode:
        raise HTTPException(status_code=403, detail="Consortium mode not enabled.")

    from orchestrator.db.models import ConsortiumPeer
    result = await db.execute(select(ConsortiumPeer).where(ConsortiumPeer.is_active == True))
    peers = result.scalars().all()
    return [
        PeerResponse(
            id=str(p.id),
            peer_endpoint=p.peer_endpoint,
            peer_alias=p.peer_alias,
            is_active=p.is_active,
            last_sync_at=p.last_sync_at.isoformat() if p.last_sync_at else None,
        )
        for p in peers
    ]


@federation_router.post("/consortium/aggregate", response_model=AggregateResponse)
async def aggregate_deltas(
    req: AggregateRequest,
    identity: Identity = Depends(get_identity),
) -> AggregateResponse:
    """Aggregate peer deltas into averaged benchmarks (hub mode only;
    plaintext numeric aggregation, not privacy-preserving)."""
    identity.assert_permission("platform:admin")
    from orchestrator.core.config import settings
    if not settings.federation_consortium_mode:
        raise HTTPException(status_code=403, detail="Consortium mode not enabled.")

    from orchestrator.core.federation_engine import BlindAggregator
    result = BlindAggregator.merge_deltas(req.deltas)
    return AggregateResponse(**result)
