"""
Modus — Compliance Report Router

Surfaces audit trail, encryption status, access controls, and data residency
information for compliance reviews (SOC 2, GDPR, HIPAA).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.config import settings
from orchestrator.db.models import AuditLog, RbacRole, User
from orchestrator.db.session import get_session

import logging

logger = logging.getLogger("modus.compliance")

router = APIRouter()


# ── Response models ──────────────────────────────────────────────────────────


class ComplianceReport(BaseModel):
    generated_at: datetime
    environment: str

    # Data residency
    data_residency: dict

    # Encryption
    encryption: dict

    # Access control
    access_control: dict

    # Audit
    audit: dict

    # Data handling
    data_handling: dict

    # Network
    network: dict


class AuditEntry(BaseModel):
    id: str
    actor_id: Optional[str] = None
    actor_ip: Optional[str] = None
    team_id: Optional[str] = None
    resource_type: str
    resource_id: Optional[str] = None
    action: str
    occurred_at: datetime
    before: Optional[dict] = None
    after: Optional[dict] = None
    chain_seq: Optional[int] = None
    entry_hash: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/compliance/report", response_model=ComplianceReport, tags=["compliance"])
async def get_compliance_report(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> ComplianceReport:
    identity.assert_permission("audit:read")

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    # Count audit events
    total_events = (await db.execute(select(func.count(AuditLog.id)))).scalar() or 0
    recent_events = (
        await db.execute(
            select(func.count(AuditLog.id)).where(AuditLog.occurred_at >= day_ago)
        )
    ).scalar() or 0

    # Count users and roles
    roles_count = (await db.execute(select(func.count(RbacRole.id)))).scalar() or 0
    users_count = (
        await db.execute(
            select(func.count(User.id)).where(User.is_active == True)  # noqa: E712
        )
    ).scalar() or 0

    # Check TLS
    tls_version = "TLS 1.2+"
    try:
        from orchestrator.core.tls import get_tls_context  # noqa: F401

        tls_version = "mTLS" if getattr(settings, "tls_mutual", False) else "TLS 1.2+"
    except (ImportError, AttributeError):
        pass  # TLS module is optional; default label stands

    db_type = "postgresql" if "postgresql" in settings.database_url else "sqlite"

    # Live audit-integrity probe: re-derive the tamper-evident chain rather
    # than merely asserting "logging_enabled". An examiner can reproduce this
    # independently via GET /audit-log/export + scripts/verify_audit_export.py.
    from orchestrator.core.audit_chain import verify_audit_chain
    from orchestrator.core.audit_checkpoint import checkpoint_signing_available
    chain_result = await verify_audit_chain(db)

    return ComplianceReport(
        generated_at=now,
        environment=settings.environment,
        data_residency={
            "storage": "self-hosted",
            "database": db_type,
            "region": "user-controlled",
            "external_calls": "optional-integrations-only",
        },
        encryption={
            "at_rest": bool(settings.encryption_key),
            "in_transit": True,
            "tls_version": tls_version,
        },
        access_control={
            "rbac_enabled": True,
            "roles_count": roles_count,
            "users_count": users_count,
            "mfa_enforced": False,
            "sso_available": False,
        },
        audit={
            "logging_enabled": True,
            "total_events": total_events,
            "retention_days": -1,  # unlimited by default (no retention policy configured)
            "recent_events_24h": recent_events,
            # Live, re-derivable integrity signals (not asserted):
            "chain_verified": chain_result.valid,
            "chain_entries_verified": chain_result.entries_checked,
            "chain_break_at_seq": chain_result.break_seq,
            "checkpoint_signing_available": checkpoint_signing_available(),
        },
        data_handling={
            "pii_scanning_available": False,
            "telemetry_sent": False,
            "third_party_data_sharing": False,
            "prompt_logging": False,
        },
        network={
            "cors_origins": settings.cors_origins,
            "rate_limiting": True,
        },
    )


@router.get("/compliance/evidence-pack", tags=["compliance"])
async def get_evidence_pack(
    team_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> dict:
    """Assemble a signed, framework-mapped regulatory evidence pack.

    Bundles the governance decision ledger (mapped to EU AI Act / NIST AI RMF /
    ISO 42001), the tamper-evident enforcement + config audit trail, and policy
    verification certificates into one Ed25519-signed manifest that an examiner
    can verify offline (scripts/verify_audit_export.py) with the public key
    alone. Requires ``audit:read``.
    """
    identity.assert_permission("audit:read")
    from orchestrator.core.evidence_pack import build_evidence_pack

    # Non-admins are scoped to their own team.
    if not identity.is_platform_admin:
        if not identity.team_ids:
            return {"format": "modus-evidence-pack-v1", "governance_decisions": [],
                    "detail": "No team access."}
        if team_id and team_id not in identity.team_ids:
            identity.assert_team_access(team_id)
        team_id = team_id or next(iter(identity.team_ids))

    return await build_evidence_pack(db, team_id=team_id, since=since, until=until)


@router.get("/compliance/audit-trail", response_model=list[AuditEntry], tags=["compliance"])
async def get_audit_trail(
    resource_type: Optional[str] = None,
    action: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[AuditEntry]:
    identity.assert_permission("audit:read")
    q = select(AuditLog)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if action:
        q = q.where(AuditLog.action == action)
    if since:
        q = q.where(AuditLog.occurred_at >= since)
    rows = (
        await db.execute(
            q.order_by(AuditLog.occurred_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return [
        AuditEntry(
            id=str(r.id),
            actor_id=r.actor_id,
            actor_ip=r.actor_ip,
            team_id=str(r.team_id) if r.team_id else None,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            action=r.action,
            occurred_at=r.occurred_at,
            before=r.before,
            after=r.after,
            chain_seq=r.chain_seq,
            entry_hash=r.entry_hash,
        )
        for r in rows
    ]
