"""
Modus — Trajectory Compliance Receipt API
==============================================
Endpoints for one-shot trajectory compliance receipts (Phase 8b).

These endpoints produce tamper-evident compliance receipts (SHA-256 hash
chain), NOT zero-knowledge proofs. The receipt binds a session's trajectory
and active policies into a commitment; it does not hide the trajectory from a
verifier. The ``zk-proofs`` route prefix is retained for backward
compatibility only.

POST /api/v1/compliance/zk-proofs/generate  — generate receipt for session
POST /api/v1/compliance/zk-proofs/verify    — verify a receipt
GET  /api/v1/compliance/zk-proofs           — list receipts for session
GET  /api/v1/compliance/zk-proofs/stats     — receipt coverage and timing stats
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity, team_scope_clause
from orchestrator.db.models import TrajectoryProof
from orchestrator.db.session import get_read_session

logger = logging.getLogger(__name__)

zk_proofs_router = APIRouter(prefix="/compliance/zk-proofs", tags=["compliance"])


# ── Request / Response schemas ───────────────────────────────────────────────

class GenerateProofRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    trajectory_steps: list[dict] = Field(..., min_length=1, max_length=500)
    active_policies: list[dict] = Field(..., min_length=1, max_length=50)


class ProofResponse(BaseModel):
    session_id: str
    proof_type: str
    proof_status: str
    proof_data: str
    public_inputs: Optional[dict] = None
    circuit_size: int
    prover_time_ms: int
    created_at: str

    class Config:
        from_attributes = True


class VerifyProofRequest(BaseModel):
    proof_data: str = Field(..., min_length=1)
    public_inputs: dict


class VerifyProofResponse(BaseModel):
    valid: bool
    proof_type: str
    message: str


class ProofStatsResponse(BaseModel):
    total_proofs: int
    valid_proofs: int
    invalid_proofs: int
    avg_prover_time_ms: float
    avg_circuit_size: float
    coverage: float = 0.0  # valid_proofs / total_proofs, 0-1


# ── Endpoints ────────────────────────────────────────────────────────────────

@zk_proofs_router.post("/generate", response_model=ProofResponse)
async def generate_proof(
    body: GenerateProofRequest,
    identity: Identity = Depends(get_identity),
):
    """Generate a tamper-evident trajectory compliance receipt for a session."""
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver

    prover = TrajectoryProver()
    result = prover.generate_proof(
        session_id=body.session_id,
        trajectory_steps=body.trajectory_steps,
        active_policies=body.active_policies,
    )

    return ProofResponse(
        session_id=result.session_id,
        proof_type=result.proof_type,
        proof_status=result.proof_status,
        proof_data=result.proof_data,
        public_inputs=result.public_inputs,
        circuit_size=result.circuit_size,
        prover_time_ms=result.prover_time_ms,
        created_at=result.created_at,
    )


@zk_proofs_router.post("/verify", response_model=VerifyProofResponse)
async def verify_proof(
    body: VerifyProofRequest,
    identity: Identity = Depends(get_identity),
):
    """Verify a trajectory compliance receipt (recompute the commitment)."""
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver

    prover = TrajectoryProver()
    valid = prover.verify_proof(body.proof_data, body.public_inputs)

    return VerifyProofResponse(
        valid=valid,
        proof_type="hash_chain",
        message="Compliance receipt verified" if valid else "Receipt verification failed",
    )


@zk_proofs_router.get("", response_model=list[ProofResponse])
async def list_proofs(
    session_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """List trajectory proofs, optionally filtered by session."""
    q = select(TrajectoryProof).order_by(TrajectoryProof.created_at.desc())

    if session_id:
        q = q.where(TrajectoryProof.session_id == session_id)
    if not identity.is_platform_admin:
        q = q.where(TrajectoryProof.team_id == identity.team_id)

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()

    return [
        ProofResponse(
            session_id=r.session_id,
            proof_type=r.proof_type,
            proof_status=r.proof_status,
            proof_data=r.proof_data,
            public_inputs=None,
            circuit_size=r.circuit_size,
            prover_time_ms=r.prover_time_ms,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@zk_proofs_router.get("/stats", response_model=ProofStatsResponse)
async def proof_stats(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Return aggregate proof statistics for the teams the caller can see."""
    scope = team_scope_clause(TrajectoryProof.team_id, identity.visible_team_ids())

    total = await db.scalar(
        select(func.count(TrajectoryProof.id)).where(scope)
    ) or 0

    valid = await db.scalar(
        select(func.count(TrajectoryProof.id))
        .where(scope, TrajectoryProof.proof_status == "valid")
    ) or 0

    invalid = await db.scalar(
        select(func.count(TrajectoryProof.id))
        .where(scope, TrajectoryProof.proof_status == "invalid")
    ) or 0

    avg_time = await db.scalar(
        select(func.avg(TrajectoryProof.prover_time_ms)).where(scope)
    ) or 0.0

    avg_circuit = await db.scalar(
        select(func.avg(TrajectoryProof.circuit_size)).where(scope)
    ) or 0.0

    return ProofStatsResponse(
        total_proofs=total,
        valid_proofs=valid,
        invalid_proofs=invalid,
        avg_prover_time_ms=round(float(avg_time), 2),
        avg_circuit_size=round(float(avg_circuit), 2),
        coverage=round(valid / total, 4) if total else 0.0,
    )
