"""
Modus — Swarm Governance API (Threshold Approval, Preview)
==================================
REST endpoints for threshold (k-of-n) swarm sign-off.

NOTE: This is threshold approval (Preview) — k-of-n sign-off. It is NOT
privacy-preserving MPC: a central evaluator reads/reconstructs the
contributions (see ``orchestrator.core.mpc_engine``). Contributions are
encrypted at rest, but there is no secure multi-party protocol.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.session import get_read_session, get_session

logger = logging.getLogger(__name__)

swarm_router = APIRouter(prefix="/governance/swarm", tags=["governance"])


# ── Request / Response schemas ────────────────────────────────────────────────

class SwarmRegisterRequest(BaseModel):
    swarm_name: str
    party_ids: list[str]
    shared_policy_ids: list[str] = []
    threshold_k: int = 2
    total_parties_n: int = 3
    evaluation_mode: str = "additive"


class SwarmResponse(BaseModel):
    id: str
    swarm_name: str
    threshold_k: int
    total_parties_n: int
    evaluation_mode: str
    is_active: bool

    class Config:
        from_attributes = True


class SessionCreateRequest(BaseModel):
    swarm_id: str


class SessionResponse(BaseModel):
    id: str
    swarm_id: str
    session_ref: str
    status: str
    contributions_received: int
    expires_at: Optional[str] = None

    class Config:
        from_attributes = True


class ContributeRequest(BaseModel):
    party_id: str
    share_data: dict


class EvaluateResponse(BaseModel):
    decision: str
    party_count: int
    threshold_met: bool
    pdr_id: Optional[str] = None


class PDRResponse(BaseModel):
    id: str
    decision: str
    party_count: int
    threshold_met: bool
    attestation_signature: Optional[str] = None
    issued_at: Optional[str] = None

    class Config:
        from_attributes = True


class PDRVerifyResponse(BaseModel):
    valid: bool
    signature_present: bool
    proof_present: bool
    decision: Optional[str] = None
    party_count: Optional[int] = None
    threshold_met: Optional[bool] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@swarm_router.post("/register", response_model=SwarmResponse)
async def register_swarm(
    req: SwarmRegisterRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> SwarmResponse:
    """Register a swarm configuration."""
    identity.assert_permission("evaluate:write")
    team_id = identity.team_ids[0] if identity.team_ids else "default"

    from orchestrator.core.config import settings
    if req.total_parties_n > settings.swarm_max_parties:
        raise HTTPException(status_code=400, detail=f"Max parties is {settings.swarm_max_parties}")

    from orchestrator.db.models import SwarmConfig
    swarm = SwarmConfig(
        team_id=team_id,
        swarm_name=req.swarm_name,
        party_ids=req.party_ids,
        shared_policy_ids=req.shared_policy_ids,
        threshold_k=req.threshold_k,
        total_parties_n=req.total_parties_n,
        evaluation_mode=req.evaluation_mode,
    )
    db.add(swarm)
    await db.flush()
    await db.commit()

    return SwarmResponse(
        id=str(swarm.id),
        swarm_name=swarm.swarm_name,
        threshold_k=swarm.threshold_k,
        total_parties_n=swarm.total_parties_n,
        evaluation_mode=swarm.evaluation_mode,
        is_active=swarm.is_active,
    )


@swarm_router.get("/list", response_model=list[SwarmResponse])
async def list_swarms(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> list[SwarmResponse]:
    """List swarms for the user's team."""
    identity.assert_permission("evaluate:read")
    from orchestrator.db.models import SwarmConfig

    q = select(SwarmConfig).where(SwarmConfig.is_active == True)
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(SwarmConfig.team_id.in_(identity.team_ids))

    result = await db.execute(q)
    swarms = result.scalars().all()
    return [
        SwarmResponse(
            id=str(s.id),
            swarm_name=s.swarm_name,
            threshold_k=s.threshold_k,
            total_parties_n=s.total_parties_n,
            evaluation_mode=s.evaluation_mode,
            is_active=s.is_active,
        )
        for s in swarms
    ]


@swarm_router.post("/sessions", response_model=SessionResponse)
async def create_session(
    req: SessionCreateRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> SessionResponse:
    """Create a swarm evaluation session."""
    identity.assert_permission("evaluate:write")
    from orchestrator.db.models import SwarmConfig, SwarmSession
    from orchestrator.core.config import settings

    # Verify swarm exists
    swarm_result = await db.execute(
        select(SwarmConfig).where(SwarmConfig.id == req.swarm_id, SwarmConfig.is_active == True)
    )
    swarm = swarm_result.scalar_one_or_none()
    if not swarm:
        raise HTTPException(status_code=404, detail="Swarm not found or inactive.")

    team_id = identity.team_ids[0] if identity.team_ids else "default"
    session = SwarmSession(
        team_id=team_id,
        swarm_id=swarm.id,
        session_ref=secrets.token_hex(16),
        status="open",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.swarm_session_ttl_minutes),
    )
    db.add(session)
    await db.flush()
    await db.commit()

    return SessionResponse(
        id=str(session.id),
        swarm_id=str(session.swarm_id),
        session_ref=session.session_ref,
        status=session.status,
        contributions_received=session.contributions_received,
        expires_at=session.expires_at.isoformat() if session.expires_at else None,
    )


@swarm_router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session_status(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> SessionResponse:
    """Get swarm session status."""
    identity.assert_permission("evaluate:read")
    from orchestrator.db.models import SwarmSession

    result = await db.execute(
        select(SwarmSession).where(SwarmSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    return SessionResponse(
        id=str(session.id),
        swarm_id=str(session.swarm_id),
        session_ref=session.session_ref,
        status=session.status,
        contributions_received=session.contributions_received,
        expires_at=session.expires_at.isoformat() if session.expires_at else None,
    )


@swarm_router.post("/sessions/{session_id}/contribute")
async def contribute(
    session_id: str,
    req: ContributeRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Submit a party contribution to a swarm session (encrypted at rest;
    read in plaintext by the evaluator — not privacy-preserving MPC)."""
    identity.assert_permission("evaluate:write")
    from orchestrator.db.models import SwarmSession, SwarmConfig, SwarmContribution
    from orchestrator.core.mpc_engine import generate_party_hash

    # Get session
    session_result = await db.execute(
        select(SwarmSession).where(SwarmSession.id == session_id, SwarmSession.status == "open")
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or not open.")

    # Check expiry
    if session.expires_at and datetime.now(timezone.utc) > session.expires_at:
        session.status = "expired"
        await db.flush()
        await db.commit()
        raise HTTPException(status_code=410, detail="Session expired.")

    # Get swarm config for salt
    swarm_result = await db.execute(
        select(SwarmConfig).where(SwarmConfig.id == session.swarm_id)
    )
    swarm = swarm_result.scalar_one_or_none()
    salt = str(swarm.id) if swarm else "default"

    # Generate party hash
    party_hash = generate_party_hash(req.party_id, salt)

    # Check for duplicate
    existing = await db.execute(
        select(SwarmContribution).where(
            SwarmContribution.session_id == session.id,
            SwarmContribution.party_hash == party_hash,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Party already contributed to this session.")

    # Encrypt share data. Fails closed: if no MODUS_ENCRYPTION_KEY is
    # configured, refuse to persist plaintext MPC shares.
    import json
    from orchestrator.core.credential_crypto import (
        CredentialEncryptionError,
        encrypt_credential,
    )
    try:
        encrypted = encrypt_credential(json.dumps(req.share_data))
    except CredentialEncryptionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    contribution = SwarmContribution(
        session_id=session.id,
        party_hash=party_hash,
        share_data=encrypted,
    )
    db.add(contribution)
    session.contributions_received += 1
    await db.flush()
    await db.commit()

    return {"status": "accepted", "contributions_received": session.contributions_received}


@swarm_router.post("/sessions/{session_id}/evaluate", response_model=EvaluateResponse)
async def evaluate_session(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> EvaluateResponse:
    """Trigger threshold (k-of-n) evaluation for a swarm session (not private MPC)."""
    identity.assert_permission("evaluate:write")
    from orchestrator.db.models import SwarmSession, SwarmConfig, SwarmContribution, PolicyDecisionRecord
    from orchestrator.core.mpc_engine import MPCPolicyEvaluator, generate_zk_proof
    from orchestrator.core.pdr_generator import generate_pdr
    from orchestrator.core.credential_crypto import decrypt_credential
    import json

    # Get session
    session_result = await db.execute(
        select(SwarmSession).where(SwarmSession.id == session_id)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session.status not in ("open", "evaluating"):
        raise HTTPException(status_code=400, detail=f"Session is {session.status}.")

    # Get swarm config
    swarm_result = await db.execute(
        select(SwarmConfig).where(SwarmConfig.id == session.swarm_id)
    )
    swarm = swarm_result.scalar_one_or_none()
    if not swarm:
        raise HTTPException(status_code=404, detail="Swarm configuration not found.")

    # Get contributions
    contrib_result = await db.execute(
        select(SwarmContribution).where(SwarmContribution.session_id == session.id)
    )
    contributions = contrib_result.scalars().all()

    if len(contributions) < swarm.threshold_k:
        raise HTTPException(
            status_code=400,
            detail=f"Need {swarm.threshold_k} contributions, got {len(contributions)}.",
        )

    # Decrypt and evaluate
    session.status = "evaluating"
    await db.flush()

    evaluator = MPCPolicyEvaluator(swarm.threshold_k, swarm.total_parties_n, swarm.evaluation_mode)

    decrypted_contributions = []
    for c in contributions:
        try:
            data = json.loads(decrypt_credential(c.share_data))
            decrypted_contributions.append(data)
        except Exception as exc:
            logger.warning(
                "Swarm session %s: could not decrypt contribution from %s; "
                "substituting empty share: %s", session_id, c.party_hash, exc
            )
            decrypted_contributions.append({"data": {}})

    mpc_result = evaluator.evaluate(decrypted_contributions)

    # Placeholder attestation (NOT a real ZK proof — see generate_zk_proof)
    mpc_proof = generate_zk_proof(mpc_result)

    # Generate PDR
    team_id = identity.team_ids[0] if identity.team_ids else "default"
    pdr_data = generate_pdr(team_id, str(swarm.id), str(session.id), mpc_result, mpc_proof)

    # Store PDR
    pdr = PolicyDecisionRecord(
        team_id=team_id,
        swarm_id=swarm.id,
        session_id=session.id,
        decision=mpc_result["decision"],
        party_count=mpc_result["party_count"],
        threshold_met=mpc_result["threshold_met"],
        mpc_proof=mpc_proof,
        attestation_signature=pdr_data.get("attestation_signature"),
    )
    db.add(pdr)

    # Update session
    session.status = "complete"
    session.mpc_result = mpc_result
    session.completed_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()

    return EvaluateResponse(
        decision=mpc_result["decision"],
        party_count=mpc_result["party_count"],
        threshold_met=mpc_result["threshold_met"],
        pdr_id=str(pdr.id),
    )


@swarm_router.get("/pdrs", response_model=list[PDRResponse])
async def list_pdrs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> list[PDRResponse]:
    """List policy decision records."""
    identity.assert_permission("evaluate:read")
    from orchestrator.db.models import PolicyDecisionRecord

    q = select(PolicyDecisionRecord).order_by(desc(PolicyDecisionRecord.issued_at))
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(PolicyDecisionRecord.team_id.in_(identity.team_ids))
    q = q.offset(offset).limit(limit)

    result = await db.execute(q)
    pdrs = result.scalars().all()
    return [
        PDRResponse(
            id=str(p.id),
            decision=p.decision,
            party_count=p.party_count,
            threshold_met=p.threshold_met,
            attestation_signature=p.attestation_signature,
            issued_at=p.issued_at.isoformat() if p.issued_at else None,
        )
        for p in pdrs
    ]


@swarm_router.get("/pdrs/{pdr_id}", response_model=PDRResponse)
async def get_pdr(
    pdr_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> PDRResponse:
    """Retrieve a policy decision record."""
    identity.assert_permission("evaluate:read")
    from orchestrator.db.models import PolicyDecisionRecord

    result = await db.execute(
        select(PolicyDecisionRecord).where(PolicyDecisionRecord.id == pdr_id)
    )
    pdr = result.scalar_one_or_none()
    if not pdr:
        raise HTTPException(status_code=404, detail="PDR not found.")

    return PDRResponse(
        id=str(pdr.id),
        decision=pdr.decision,
        party_count=pdr.party_count,
        threshold_met=pdr.threshold_met,
        attestation_signature=pdr.attestation_signature,
        issued_at=pdr.issued_at.isoformat() if pdr.issued_at else None,
    )


@swarm_router.post("/pdrs/{pdr_id}/verify", response_model=PDRVerifyResponse)
async def verify_pdr(
    pdr_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
) -> PDRVerifyResponse:
    """Verify PDR signature and proof."""
    identity.assert_permission("evaluate:read")
    from orchestrator.db.models import PolicyDecisionRecord
    from orchestrator.core.pdr_generator import verify_pdr as _verify

    result = await db.execute(
        select(PolicyDecisionRecord).where(PolicyDecisionRecord.id == pdr_id)
    )
    pdr = result.scalar_one_or_none()
    if not pdr:
        raise HTTPException(status_code=404, detail="PDR not found.")

    pdr_data = {
        "decision": pdr.decision,
        "party_count": pdr.party_count,
        "threshold_met": pdr.threshold_met,
        "attestation_signature": pdr.attestation_signature,
        "mpc_proof": pdr.mpc_proof,
    }
    verify_result = _verify(pdr_data)
    return PDRVerifyResponse(**verify_result)
