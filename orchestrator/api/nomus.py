"""
Modus — Nomus Regulatory Engine API
==========================================
REST endpoints for Nomus integration management.

GET  /api/v1/admin/nomus/status          — connection status + ruleset info
POST /api/v1/admin/nomus/sync            — manually trigger ruleset sync
POST /api/v1/admin/nomus/test            — test Nomus connectivity
GET  /api/v1/admin/nomus/regulations     — list all regulations in current ruleset
GET  /api/v1/admin/nomus/check/{entry_id} — check a CoT entry against regulations

Optional integration: inactive unless MODUS_NOMUS_URL is configured.
Customer data NEVER leaves their infrastructure (Law 4).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.nomus_client import (
    check_compliance,
    get_cached_policies,
    get_status,
    sync_ruleset,
    test_connectivity,
)
from orchestrator.db.models import CoTLedgerEntry
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/nomus", tags=["nomus"])


# ── Response schemas ─────────────────────────────────────────────────────────

class NomusStatusResponse(BaseModel):
    status: str
    configured: bool
    nomus_url: str | None = None
    version: str | None = None
    state_hash: str | None = None
    last_sync: str | None = None
    next_sync: str | None = None
    last_error: str | None = None
    auto_sync: bool = True
    sync_interval_seconds: int = 21600
    sync_count: int = 0
    policy_count: int = 0
    jurisdictions: list[str] = []
    categories: list[str] = []
    severity_counts: dict[str, int] = {}


class SyncResponse(BaseModel):
    success: bool
    version: str | None = None
    policy_count: int = 0
    state_hash: str | None = None
    skipped: bool = False
    error: str | None = None


class TestResponse(BaseModel):
    reachable: bool
    nomus_version: str | None = None
    latency_ms: float | None = None
    error: str | None = None


class RegulationSummary(BaseModel):
    id: str
    name: str
    version: str = ""
    articles_count: int = 0
    articles: list = []


class RegulationsResponse(BaseModel):
    version: str | None = None
    regulations: list[RegulationSummary] = []
    total_rules: int = 0


class ComplianceCheckResponse(BaseModel):
    compliant: bool
    violations: list = []
    warnings: list = []
    policies_evaluated: int = 0
    state_hash: str | None = None
    ruleset_version: str | None = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/status", response_model=NomusStatusResponse)
async def nomus_status(identity: Identity = Depends(get_identity)):
    """Get Nomus connection status and ruleset info."""
    return await get_status()


@router.post("/sync", response_model=SyncResponse)
async def nomus_sync(identity: Identity = Depends(get_identity)):
    """Manually trigger a Nomus ruleset sync."""
    result = await sync_ruleset()
    return SyncResponse(**result)


@router.post("/test", response_model=TestResponse)
async def nomus_test(identity: Identity = Depends(get_identity)):
    """Test Nomus connectivity (no data sync)."""
    result = await test_connectivity()
    return TestResponse(**result)


@router.get("/regulations", response_model=RegulationsResponse)
async def nomus_regulations(identity: Identity = Depends(get_identity)):
    """List all regulations (grouped by jurisdiction from cached policies)."""
    policies = get_cached_policies()
    if not policies:
        return RegulationsResponse(version=None, regulations=[], total_rules=0)

    # Group policies by jurisdiction
    by_jurisdiction: dict[str, list[dict]] = {}
    for p in policies:
        j = p.get("jurisdiction", "unknown")
        by_jurisdiction.setdefault(j, []).append(p)

    regulations = []
    total_rules = len(policies)
    for jurisdiction, rules in sorted(by_jurisdiction.items()):
        # Group by category within jurisdiction
        categories = set(r.get("category", "") for r in rules)
        articles = [
            {
                "id": cat,
                "title": cat.replace("_", " ").title(),
                "requirements_count": sum(1 for r in rules if r.get("category") == cat),
                "risk_levels": list(set(
                    r.get("severity", "medium") for r in rules if r.get("category") == cat
                )),
                "tags": list(set(
                    r.get("ruleKey", "") for r in rules if r.get("category") == cat
                ))[:10],
            }
            for cat in sorted(categories) if cat
        ]
        regulations.append(RegulationSummary(
            id=jurisdiction.lower().replace("-", "_"),
            name=jurisdiction,
            version="",
            articles_count=len(articles),
            articles=articles,
        ))

    from orchestrator.core.nomus_client import get_cached_state_hash
    return RegulationsResponse(
        version=get_cached_state_hash(),
        regulations=regulations,
        total_rules=total_rules,
    )


@router.get("/check/{cot_entry_id}", response_model=ComplianceCheckResponse)
async def nomus_check_entry(
    cot_entry_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Check a CoT ledger entry against current regulatory ruleset.

    Runs LOCALLY — no customer data sent to Nomus.
    """
    # Fetch the CoT entry from the database
    result = await db.execute(
        select(CoTLedgerEntry).where(CoTLedgerEntry.id == cot_entry_id)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="CoT ledger entry not found")

    # Build dict for compliance check
    entry_dict = {
        "decision_type": entry.decision_type,
        "decision_summary": entry.decision_summary,
        "regulatory_tags": entry.regulatory_tags,
        "rules_evaluated": entry.rules_evaluated,
        "reasoning_steps": entry.reasoning_steps,
    }

    result = await check_compliance(entry_dict)
    return ComplianceCheckResponse(**result)
