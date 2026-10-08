"""
Modus — Audit Log Router

Surfaces the AuditLog table for dashboard consumers (pricing history,
config change trail, etc). RBAC: platform admin or team member with
matching team_id may read entries scoped to their team.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import AuditLog
from orchestrator.db.session import get_session

router = APIRouter()


class AuditEntry(BaseModel):
    id: str
    actor_id: str
    actor_ip: Optional[str] = None
    team_id: Optional[str] = None
    resource_type: str
    resource_id: Optional[str] = None
    action: str
    before: Optional[dict] = None
    after: Optional[dict] = None
    occurred_at: datetime
    chain_seq: Optional[int] = None
    entry_hash: Optional[str] = None


class ChainVerifyResponse(BaseModel):
    valid: bool
    entries_checked: int
    error: Optional[str] = None
    break_seq: Optional[int] = None


@router.get("/audit-log", response_model=list[AuditEntry])
async def list_audit_log(
    resource_type: Optional[str] = Query(None, description="Filter by resource_type (e.g. pricing_override)"),
    resource_id: Optional[str] = Query(None, description="Filter by specific resource_id"),
    actor_id: Optional[str] = Query(None, description="Filter by actor_id"),
    limit: int = Query(50, ge=1, le=500),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[AuditEntry]:
    """List audit log entries, newest first.

    Non-platform-admin callers are scoped to their team_ids. Platform
    admins see all entries (including team-less / global ones).
    """
    q = select(AuditLog)

    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)

    if not identity.is_platform_admin:
        # Team members see only their team's audit entries
        if identity.team_ids:
            q = q.where(AuditLog.team_id.in_(identity.team_ids))
        else:
            return []

    q = q.order_by(AuditLog.occurred_at.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    return [
        AuditEntry(
            id=str(r.id),
            actor_id=r.actor_id,
            actor_ip=r.actor_ip,
            team_id=r.team_id,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            action=r.action,
            before=r.before,
            after=r.after,
            occurred_at=r.occurred_at,
            chain_seq=r.chain_seq,
            entry_hash=r.entry_hash,
        )
        for r in rows
    ]


@router.get("/audit-log/verify", response_model=ChainVerifyResponse)
async def verify_chain(
    limit: Optional[int] = Query(
        None, ge=1, description="Verify only the most recent N chained entries"
    ),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> ChainVerifyResponse:
    """Re-derive the audit hash chain and report integrity.

    Any edit, deletion, or reordering of a chained audit row is detected and
    reported with the exact chain_seq where the break occurs. Requires the
    ``audit:read`` permission.
    """
    identity.assert_permission("audit:read")
    from orchestrator.core.audit_chain import verify_audit_chain

    result = await verify_audit_chain(db, limit=limit)
    return ChainVerifyResponse(**result.as_dict())


@router.get("/audit-log/public-key")
async def audit_public_key(
    identity: Identity = Depends(get_identity),
) -> dict:
    """Return the audit-chain checkpoint public key (hex), for independent
    verification of signed exports. Null if no signing key is configured."""
    identity.assert_permission("audit:read")
    from orchestrator.core.audit_checkpoint import public_key_hex, checkpoint_signing_available

    return {
        "public_key": public_key_hex(),
        "signing_available": checkpoint_signing_available(),
        "algorithm": "ed25519",
    }


@router.post("/audit-log/checkpoint")
async def create_audit_checkpoint(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> dict:
    """Sign and persist a checkpoint over the current chain head.

    Requires ``audit:write``. Returns the checkpoint, or a status when signing
    is unavailable (no key configured) or the chain is empty.
    """
    identity.assert_permission("audit:write")
    from orchestrator.core.audit_checkpoint import create_checkpoint, checkpoint_signing_available

    if not checkpoint_signing_available():
        return {
            "status": "unavailable",
            "detail": "No checkpoint signing key configured "
                      "(set MODUS_ATTESTATION_KEY or MODUS_ENCRYPTION_KEY).",
        }
    cp = await create_checkpoint(db)
    if cp is None:
        return {"status": "empty", "detail": "No chained audit entries to checkpoint."}
    return {"status": "ok", "checkpoint": cp}


@router.get("/audit-log/export")
async def export_audit_chain(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> dict:
    """Export the full audit chain as a signed, offline-verifiable manifest.

    The manifest carries every chained entry (with the canonical timestamp used
    in hashing), the latest signed checkpoint, and the public key — everything
    ``scripts/verify_audit_export.py`` needs to verify integrity air-gapped,
    with no access to this server. Requires ``audit:read``.
    """
    identity.assert_permission("audit:read")
    from sqlalchemy import desc
    from orchestrator.core.audit_chain import canonical_timestamp
    from orchestrator.core.audit_checkpoint import (
        public_key_hex, create_checkpoint, checkpoint_signing_available,
    )
    from orchestrator.db.models import AuditCheckpoint

    q = (
        select(AuditLog)
        .where(AuditLog.chain_seq.isnot(None))
        .order_by(AuditLog.chain_seq.asc())
    )
    rows = (await db.execute(q)).scalars().all()

    chain = [
        {
            "chain_seq": r.chain_seq,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
            "actor_id": r.actor_id,
            "actor_ip": r.actor_ip,
            "team_id": r.team_id,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "action": r.action,
            "before": r.before,
            "after": r.after,
            "occurred_at": canonical_timestamp(r.occurred_at),
        }
        for r in rows
    ]

    # Sign a fresh checkpoint over the head if possible, else fall back to the
    # most recent stored one.
    checkpoint = None
    if checkpoint_signing_available() and chain:
        checkpoint = await create_checkpoint(db)
    if checkpoint is None:
        last_cp = (await db.execute(
            select(AuditCheckpoint).order_by(desc(AuditCheckpoint.chain_seq)).limit(1)
        )).scalar_one_or_none()
        if last_cp is not None:
            from orchestrator.core.audit_checkpoint import _cp_dict
            checkpoint = _cp_dict(last_cp)

    return {
        "format": "modus-audit-export-v1",
        "public_key": public_key_hex(),
        "algorithm": "ed25519",
        "entry_count": len(chain),
        "chain": chain,
        "checkpoint": checkpoint,
        "verification": (
            "Verify offline with: python scripts/verify_audit_export.py <this-file.json>  "
            "(stdlib only — no dependencies). It re-derives every entry hash, checks the "
            "chain linkage, and verifies the checkpoint signature against the embedded "
            "public key."
        ),
    }
