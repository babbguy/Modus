"""
Modus — Policy Prover API
================================
Endpoints for the Neuro-Symbolic Policy Prover.

POST /api/v1/policies/verify       — verify a policy YAML
GET  /api/v1/policies/{id}/proof   — get proof certificate for existing policy
POST /api/v1/policies/verify-batch — verify multiple policies
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import PolicyProof
from orchestrator.db.session import get_read_session, get_session

logger = logging.getLogger(__name__)

prover_router = APIRouter(prefix="/policies", tags=["prover"])


# ── Request / Response schemas ────────────────────────────────────────────────

class VerifyRequest(BaseModel):
    policy_yaml: str = Field(..., min_length=1, max_length=50000)
    method: str = Field("auto", pattern="^(auto|bounded|smt)$")
    policy_id: Optional[str] = None


class ProofResponse(BaseModel):
    policy_id: str
    policy_hash: str
    proof_type: str
    status: str
    statement: str
    counterexample: Optional[dict] = None
    variables_checked: int
    max_depth: int
    solver_time_ms: int
    proven_at: str

    class Config:
        from_attributes = True


class BatchVerifyRequest(BaseModel):
    policies: list[VerifyRequest] = Field(..., min_length=1, max_length=50)


class BatchProofResponse(BaseModel):
    results: list[ProofResponse]
    total: int
    proven: int
    disproven: int
    sampled: int = 0


# ── Endpoints ─────────────────────────────────────────────────────────────────

@prover_router.post("/verify", response_model=ProofResponse)
async def verify_policy(
    body: VerifyRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Verify a policy YAML — returns proof certificate."""
    import asyncio
    import json
    from orchestrator.core.policy_prover import prove_policy, generate_proof_certificate

    # Proving is CPU-bound (bounded enumeration up to timeout_seconds, or Z3)
    # — run in a worker thread so the event loop keeps serving requests.
    result = await asyncio.to_thread(
        prove_policy,
        policy_yaml=body.policy_yaml,
        policy_id=body.policy_id or "",
        method=body.method,
    )

    # Persist proof to policy_proofs table
    if result.policy_id:
        cert = generate_proof_certificate(result)
        proof_row = PolicyProof(
            policy_id=result.policy_id,
            proof_type=result.proof_type,
            proof_status=result.status,
            proof_certificate=json.dumps(cert),
            counterexample=json.dumps(result.counterexample) if result.counterexample else None,
            variables_checked=result.variables_checked,
            max_depth=result.max_depth,
            solver_time_ms=result.solver_time_ms,
        )
        db.add(proof_row)
        await db.flush()

    return ProofResponse(
        policy_id=result.policy_id,
        policy_hash=result.policy_hash,
        proof_type=result.proof_type,
        status=result.status,
        statement=result.statement,
        counterexample=result.counterexample,
        variables_checked=result.variables_checked,
        max_depth=result.max_depth,
        solver_time_ms=result.solver_time_ms,
        proven_at=result.proven_at,
    )


@prover_router.get("/{policy_id}/proof", response_model=ProofResponse)
async def get_proof(
    policy_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Get the proof certificate for an existing policy."""
    proof = await db.execute(
        select(PolicyProof).where(PolicyProof.policy_id == policy_id)
        .order_by(PolicyProof.proven_at.desc())
        .limit(1)
    )
    row = proof.scalars().first()
    if row is None:
        raise HTTPException(404, "No proof found for this policy")

    import json as _json
    raw_cert = row.proof_certificate or "{}"
    cert = _json.loads(raw_cert) if isinstance(raw_cert, str) else raw_cert
    return ProofResponse(
        policy_id=row.policy_id,
        policy_hash=cert.get("policy_hash", ""),
        proof_type=row.proof_type or "",
        status=row.proof_status or "unknown",
        statement=cert.get("statement", ""),
        counterexample=cert.get("counterexample"),
        variables_checked=row.variables_checked or 0,
        max_depth=row.max_depth or 0,
        solver_time_ms=row.solver_time_ms or 0,
        proven_at=row.proven_at.isoformat() if row.proven_at else "",
    )


@prover_router.post("/verify-batch", response_model=BatchProofResponse)
async def verify_batch(
    body: BatchVerifyRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Verify multiple policies in one call."""
    import asyncio
    import json
    from orchestrator.core.policy_prover import prove_policy, generate_proof_certificate

    results = []
    proven_count = 0
    disproven_count = 0
    sampled_count = 0

    for item in body.policies:
        # Each prove is CPU-bound; a 50-policy batch could otherwise block
        # the event loop for minutes.
        result = await asyncio.to_thread(
            prove_policy,
            policy_yaml=item.policy_yaml,
            policy_id=item.policy_id or "",
            method=item.method,
        )

        # Persist proof
        if result.policy_id:
            cert = generate_proof_certificate(result)
            proof_row = PolicyProof(
                policy_id=result.policy_id,
                proof_type=result.proof_type,
                proof_status=result.status,
                proof_certificate=json.dumps(cert),
                counterexample=json.dumps(result.counterexample) if result.counterexample else None,
                variables_checked=result.variables_checked,
                max_depth=result.max_depth,
                solver_time_ms=result.solver_time_ms,
            )
            db.add(proof_row)

        resp = ProofResponse(
            policy_id=result.policy_id,
            policy_hash=result.policy_hash,
            proof_type=result.proof_type,
            status=result.status,
            statement=result.statement,
            counterexample=result.counterexample,
            variables_checked=result.variables_checked,
            max_depth=result.max_depth,
            solver_time_ms=result.solver_time_ms,
            proven_at=result.proven_at,
        )
        results.append(resp)
        if result.status == "proven":
            proven_count += 1
        elif result.status == "sampled":
            sampled_count += 1
        elif result.status == "disproven":
            disproven_count += 1

    await db.flush()

    return BatchProofResponse(
        results=results,
        total=len(results),
        proven=proven_count,
        sampled=sampled_count,
        disproven=disproven_count,
    )
