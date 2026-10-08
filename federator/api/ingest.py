"""
Modus Federator — Delta Ingest API
=========================================
POST /v1/deltas — accept encrypted constitution deltas with ZK proofs.

CRITICAL: The encrypted_payload is stored as-is. NEVER decrypted.
Only the metadata signals are used for aggregation.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from federator.config import settings
from federator.core.auth import FederatorIdentity, get_identity, require_participant
from federator.core.zk_verifier import SigmaProof, verify_sigma_proof, compute_merkle_leaf
from federator.db.session import get_session

logger = logging.getLogger(__name__)

ingest_router = APIRouter(prefix="/v1", tags=["ingest"])

# Rate limiter instance (created in main.py, injected here)
_submit_limiter = None


def set_submit_limiter(limiter) -> None:
    global _submit_limiter
    _submit_limiter = limiter


# ── Request / Response schemas ────────────────────────────────────────────────

class ZKProofPayload(BaseModel):
    commitment: str = Field(..., min_length=64, max_length=64)
    challenge: str = Field(..., min_length=64, max_length=64)
    response: str = Field(..., min_length=64, max_length=64)
    public_inputs: Optional[dict] = None


class DeltaSubmission(BaseModel):
    nonce: str = Field(..., min_length=16, max_length=64)
    industry_type: str = Field(..., min_length=2, max_length=64)
    encrypted_payload: str = Field(
        ...,
        description="Base64-encoded encrypted blob — NEVER decrypted by federator",
    )
    zk_proof: ZKProofPayload
    fitness_improvement: float = Field(..., ge=-1.0, le=1.0)
    generation_span: int = Field(..., ge=1, le=100)
    gene_count: int = Field(..., ge=1, le=1000)
    timestamp: Optional[str] = None


class DeltaAccepted(BaseModel):
    status: str = "accepted"
    delta_id: str
    merkle_leaf_hash: str
    epoch_week: str


# ── Endpoint ──────────────────────────────────────────────────────────────────

@ingest_router.post("/deltas", response_model=DeltaAccepted)
async def submit_delta(
    submission: DeltaSubmission,
    identity: FederatorIdentity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> DeltaAccepted:
    """
    Accept an encrypted constitution delta with ZK proof.

    Requires participant tier. Rate limited per identity fingerprint.

    The encrypted_payload is stored as-is — NEVER decrypted.
    Only metadata (fitness, gene_count, generation_span) is used.
    """
    # ── Tier check ────────────────────────────────────────────────────────
    require_participant(identity)

    # ── Rate limit ────────────────────────────────────────────────────────
    if _submit_limiter and not _submit_limiter.allow(identity.identity_fingerprint):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Try again later.",
        )

    # ── Industry validation ───────────────────────────────────────────────
    if submission.industry_type not in settings.allowed_industries:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown industry type: {submission.industry_type}. "
                   f"Allowed: {settings.allowed_industries}",
        )

    # ── Payload size check ────────────────────────────────────────────────
    try:
        payload_bytes = base64.b64decode(submission.encrypted_payload)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid base64 in encrypted_payload")

    if len(payload_bytes) > settings.max_payload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Payload exceeds {settings.max_payload_bytes} bytes",
        )

    # ── ZK proof verification ────────────────────────────────────────────
    proof = SigmaProof(
        commitment=submission.zk_proof.commitment,
        challenge=submission.zk_proof.challenge,
        response=submission.zk_proof.response,
        public_inputs=submission.zk_proof.public_inputs or {},
    )
    verification = verify_sigma_proof(
        proof,
        submission.fitness_improvement,
        submission.gene_count,
        submission.generation_span,
    )
    if not verification.valid:
        raise HTTPException(
            status_code=422,
            detail=f"ZK proof verification failed: {verification.reason}",
        )

    # ── Compute Merkle leaf ───────────────────────────────────────────────
    merkle_leaf = compute_merkle_leaf(
        submission.nonce,
        proof.commitment,
        submission.fitness_improvement,
        submission.gene_count,
        submission.generation_span,
    )

    # ── Compute epoch week ────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    epoch_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

    # ── Store ─────────────────────────────────────────────────────────────
    from federator.db.models import EncryptedDelta

    delta = EncryptedDelta(
        nonce=submission.nonce,
        industry_type=submission.industry_type,
        encrypted_payload=payload_bytes,
        zk_proof_commitment=proof.commitment,
        zk_proof_challenge=proof.challenge,
        zk_proof_response=proof.response,
        zk_public_inputs=proof.public_inputs,
        fitness_improvement=submission.fitness_improvement,
        generation_span=submission.generation_span,
        gene_count=submission.gene_count,
        merkle_leaf_hash=merkle_leaf,
        epoch_week=epoch_week,
    )
    db.add(delta)
    await db.flush()

    logger.info(
        "Delta accepted: industry=%s gene_count=%d fitness=%.4f week=%s",
        submission.industry_type, submission.gene_count,
        submission.fitness_improvement, epoch_week,
    )

    return DeltaAccepted(
        delta_id=delta.id,
        merkle_leaf_hash=merkle_leaf,
        epoch_week=epoch_week,
    )
