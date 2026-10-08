"""
Modus — Attestation Compliance API
======================================
Endpoints for Verifiable Enforcement Attestations (Phase 3).

POST /api/v1/compliance/verify-attestation   — verify a single attestation
POST /api/v1/compliance/verify-batch         — verify Merkle root + inclusion proof
GET  /api/v1/compliance/attestations         — batch export for auditors
GET  /api/v1/compliance/merkle-roots         — list Merkle roots
GET  /api/v1/compliance/attestation-stats    — counts (total / verified / pending)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.attestation_engine import AttestationSigner, resolve_attestation_key
from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.merkle import MerkleTree
from orchestrator.db.models import (
    EnforcementAttestation,
    MerkleRoot as MerkleRootModel,
    PolicyDecision,
)
from orchestrator.db.session import get_read_session

logger = logging.getLogger(__name__)

attestation_router = APIRouter(prefix="/compliance", tags=["compliance"])


# ── Request / Response schemas ───────────────────────────────────────────────

class VerifyRequest(BaseModel):
    decision_id: str


class VerifyBatchRequest(BaseModel):
    attestation_hash: str
    merkle_proof: list
    merkle_root: str


class AttestationResponse(BaseModel):
    id: str
    decision_id: str
    attestation_hash: str
    signature: str
    nonce: str
    key_source: str
    algorithm: str
    merkle_batch_id: Optional[str] = None
    merkle_root: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MerkleRootResponse(BaseModel):
    id: str
    batch_id: str
    root_hash: str
    leaf_count: int
    period_start: datetime
    period_end: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class AttestationStatsResponse(BaseModel):
    total: int = 0
    verified: int = 0
    pending: int = 0


# ── Verify single attestation ───────────────────────────────────────────────

@attestation_router.post("/verify-attestation")
async def verify_attestation(
    body: VerifyRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Verify a single enforcement attestation by decision ID."""
    key = resolve_attestation_key()
    if key is None:
        raise HTTPException(503, "Attestation signing key not available")

    signer = AttestationSigner(key)

    result = await db.execute(
        select(EnforcementAttestation).where(
            EnforcementAttestation.decision_id == body.decision_id
        )
    )
    attestation = result.scalars().first()
    if attestation is None:
        raise HTTPException(404, "Attestation not found for this decision")

    import json
    try:
        payload = json.loads(attestation.payload_json)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(422, "Attestation payload contains invalid JSON")
    valid = signer.verify_attestation(payload, attestation.signature)

    return {
        "valid": valid,
        "decision_id": body.decision_id,
        "algorithm": attestation.algorithm,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Verify batch (Merkle proof) ─────────────────────────────────────────────

@attestation_router.post("/verify-batch")
async def verify_batch(
    body: VerifyBatchRequest,
    identity: Identity = Depends(get_identity),
):
    """Verify a Merkle root inclusion proof for an attestation hash."""
    valid = MerkleTree.verify_proof(
        body.attestation_hash, body.merkle_proof, body.merkle_root,
    )
    return {
        "valid": valid,
        "merkle_root": body.merkle_root,
    }


# ── Attestation export ──────────────────────────────────────────────────────

@attestation_router.get("/attestations", response_model=list[AttestationResponse])
async def list_attestations(
    start: datetime = Query(..., description="Period start (inclusive)"),
    end: datetime = Query(..., description="Period end (exclusive)"),
    team_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Batch export of attestations for auditors."""
    q = (
        select(EnforcementAttestation)
        .where(
            and_(
                EnforcementAttestation.created_at >= start,
                EnforcementAttestation.created_at < end,
            )
        )
        .order_by(EnforcementAttestation.created_at.desc())
    )

    # Filter by team through the PolicyDecision FK
    if team_id:
        q = q.join(
            PolicyDecision,
            PolicyDecision.id == EnforcementAttestation.decision_id,
        ).where(PolicyDecision.team_id == team_id)
    elif not identity.is_platform_admin:
        q = q.join(
            PolicyDecision,
            PolicyDecision.id == EnforcementAttestation.decision_id,
        ).where(PolicyDecision.team_id == identity.team_id)

    q = q.limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


# ── Merkle roots ─────────────────────────────────────────────────────────────

@attestation_router.get("/merkle-roots", response_model=list[MerkleRootResponse])
async def list_merkle_roots(
    start: datetime = Query(..., description="Period start (inclusive)"),
    end: datetime = Query(..., description="Period end (exclusive)"),
    limit: int = Query(50, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List Merkle roots within a time range."""
    q = (
        select(MerkleRootModel)
        .where(
            and_(
                MerkleRootModel.created_at >= start,
                MerkleRootModel.created_at < end,
            )
        )
        .order_by(MerkleRootModel.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(q)
    return result.scalars().all()


# ── Attestation stats ────────────────────────────────────────────────────────

@attestation_router.get("/attestation-stats", response_model=AttestationStatsResponse)
async def attestation_stats(
    team_id: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Total / verified / pending attestation counts for a team."""
    base = select(func.count().label("cnt"))

    # Determine effective team filter
    effective_team = team_id
    if not effective_team and not identity.is_platform_admin:
        effective_team = identity.team_id

    if effective_team:
        base = base.select_from(EnforcementAttestation).join(
            PolicyDecision,
            PolicyDecision.id == EnforcementAttestation.decision_id,
        ).where(PolicyDecision.team_id == effective_team)
    else:
        base = base.select_from(EnforcementAttestation)

    # Total count
    total_result = await db.execute(base)
    total = total_result.scalar() or 0

    # Verified count (verified_at is not null)
    verified_q = base.where(EnforcementAttestation.verified_at.isnot(None))
    verified_result = await db.execute(verified_q)
    verified = verified_result.scalar() or 0

    pending = total - verified

    return AttestationStatsResponse(
        total=total,
        verified=verified,
        pending=pending,
    )
