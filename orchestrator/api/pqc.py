"""
Modus — Post-Quantum Attestation API
=========================================
Endpoints for the attestation layer (Phase 8a).

The default attestation tier is an HMAC-SHA-512 hash-chain commitment (a
symmetric MAC — quantum-resistant integrity, not a publicly-verifiable
signature). Real ML-DSA-65 signatures are an optional add-on that requires
the ``pqcrypto`` package. The ``/status`` endpoint reports the algorithm
actually in force at runtime.

GET  /api/v1/compliance/pqc/status   — attestation status and tier info
GET  /api/v1/compliance/pqc/score    — attestation coverage score
POST /api/v1/compliance/pqc/verify   — verify an attestation value
POST /api/v1/compliance/pqc/migrate  — trigger backfill migration (admin)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import EnforcementAttestation
from orchestrator.db.session import get_session, get_read_session

logger = logging.getLogger(__name__)

pqc_router = APIRouter(prefix="/compliance/pqc", tags=["compliance"])


# ── Response schemas ─────────────────────────────────────────────────────────

class PQCStatusResponse(BaseModel):
    enabled: bool
    tier: str
    pqc_algorithm: str
    total_attestations: int
    pqc_signed: int
    migration_pending: int


class PQCScoreResponse(BaseModel):
    compliance_score: float
    total_attestations: int
    pqc_signed: int
    migration_in_progress: bool


class PQCVerifyRequest(BaseModel):
    attestation_id: str
    payload_json: str
    pqc_signature: str
    pqc_algorithm: str


class PQCVerifyResponse(BaseModel):
    valid: bool
    algorithm: str
    message: str


class PQCMigrateResponse(BaseModel):
    migrated: int
    skipped: int
    errors: int


# ── Endpoints ────────────────────────────────────────────────────────────────

@pqc_router.get("/status", response_model=PQCStatusResponse)
async def pqc_status(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Return PQC attestation status and tier info."""
    from orchestrator.core.config import get_settings
    settings = get_settings()

    total = await db.scalar(
        select(func.count(EnforcementAttestation.id))
    ) or 0

    pqc_signed = await db.scalar(
        select(func.count(EnforcementAttestation.id))
        .where(EnforcementAttestation.pqc_signature.isnot(None))
    ) or 0

    # Report the algorithm that would ACTUALLY be used at runtime — a "real"
    # tier without pqcrypto installed reports unavailable, never ml-dsa-65.
    from orchestrator.core.pqc_signer import resolve_algorithm

    return PQCStatusResponse(
        enabled=settings.pqc_attestation_enabled,
        tier=settings.pqc_attestation_tier,
        pqc_algorithm=resolve_algorithm(settings.pqc_attestation_tier),
        total_attestations=total,
        pqc_signed=pqc_signed,
        migration_pending=total - pqc_signed,
    )


@pqc_router.get("/score", response_model=PQCScoreResponse)
async def pqc_score(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_read_session),
):
    """Return PQC compliance score (% of attestations with PQC signatures)."""
    total = await db.scalar(
        select(func.count(EnforcementAttestation.id))
    ) or 0

    pqc_signed = await db.scalar(
        select(func.count(EnforcementAttestation.id))
        .where(EnforcementAttestation.pqc_signature.isnot(None))
    ) or 0

    score = (pqc_signed / total * 100) if total > 0 else 100.0

    return PQCScoreResponse(
        compliance_score=round(score, 2),
        total_attestations=total,
        pqc_signed=pqc_signed,
        migration_in_progress=False,
    )


@pqc_router.post("/verify", response_model=PQCVerifyResponse)
async def verify_pqc_attestation(
    body: PQCVerifyRequest,
    identity: Identity = Depends(get_identity),
):
    """Verify a PQC attestation signature."""
    from orchestrator.core.pqc_signer import PQCAttestationSigner
    from orchestrator.core.attestation_engine import resolve_attestation_key
    from orchestrator.core.config import get_settings

    key = resolve_attestation_key()
    if not key:
        raise HTTPException(500, "No attestation key configured")

    try:
        signer = PQCAttestationSigner(
            attestation_key=key, tier=get_settings().pqc_attestation_tier
        )
    except RuntimeError as exc:
        # tier="real" without pqcrypto fails loudly — never a silent downgrade.
        raise HTTPException(500, str(exc)) from exc
    valid = signer.verify(body.payload_json, body.pqc_signature, body.pqc_algorithm)

    return PQCVerifyResponse(
        valid=valid,
        algorithm=body.pqc_algorithm,
        message="Attestation value verified" if valid else "Attestation verification failed",
    )


@pqc_router.post("/migrate", response_model=PQCMigrateResponse)
async def trigger_migration(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Trigger PQC migration of existing attestations (admin only)."""
    if not identity.is_platform_admin:
        raise HTTPException(403, "PQC migration requires platform admin")

    from orchestrator.core.pqc_signer import PQCAttestationSigner
    from orchestrator.core.pqc_migration import migrate_attestations
    from orchestrator.core.attestation_engine import resolve_attestation_key
    from orchestrator.core.config import get_settings

    key = resolve_attestation_key()
    if not key:
        raise HTTPException(500, "No attestation key configured")

    try:
        signer = PQCAttestationSigner(
            attestation_key=key, tier=get_settings().pqc_attestation_tier
        )
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    result = await migrate_attestations(db, signer)

    return PQCMigrateResponse(**result)
