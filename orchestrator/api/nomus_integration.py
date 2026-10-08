"""
Modus — Nomus Integration API
==========================================
Integration-facing endpoints for the Nomus regulatory engine.

GET  /api/v1/integrations/nomus/status   — connection status + sync info
GET  /api/v1/integrations/nomus/policies — applicable regulatory policies

Optional integration: inactive unless MODUS_NOMUS_URL is configured.
Customer data NEVER leaves their infrastructure (Law 2).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.nomus_client import (
    get_cached_policies,
    get_cached_state_hash,
    get_status,
)

logger = logging.getLogger(__name__)

nomus_integration_router = APIRouter(
    prefix="/integrations/nomus",
    tags=["nomus"],
)


# ── Response schemas ────────────────────────────────────────────────────────

class NomusIntegrationStatusResponse(BaseModel):
    configured: bool
    connected: bool
    nomus_url: str | None = None
    last_sync: str | None = None
    last_error: str | None = None
    regulation_count: int = 0
    policy_count: int = 0
    compliance_score: float | None = None
    jurisdictions: list[str] = []
    categories: list[str] = []
    severity_counts: dict[str, int] = {}
    auto_sync: bool = True
    sync_interval_seconds: int = 21600
    sync_count: int = 0
    version: str | None = None
    state_hash: str | None = None


class NomusPolicySummary(BaseModel):
    rule_key: str
    jurisdiction: str
    category: str
    severity: str
    effect: str
    human_summary: str
    legal_reference: str


class NomusPoliciesResponse(BaseModel):
    total: int = 0
    version: str | None = None
    state_hash: str | None = None
    policies: list[NomusPolicySummary] = []
    issued_at: str


# ── Endpoints ────────────────────────────────────────────────────────────────

@nomus_integration_router.get(
    "/status",
    response_model=NomusIntegrationStatusResponse,
)
async def nomus_integration_status(
    identity: Identity = Depends(get_identity),
) -> NomusIntegrationStatusResponse:
    """Return Nomus integration status for the Connections dashboard."""
    status_data = await get_status()

    configured = status_data.get("configured", False)
    conn_status = status_data.get("status", "not_configured")
    connected = conn_status == "connected"

    policy_count = status_data.get("policy_count", 0)
    severity_counts = status_data.get("severity_counts", {})
    jurisdictions = status_data.get("jurisdictions", [])

    # Derive a compliance score from severity distribution.
    # Score = 1.0 when no critical/high policies are unresolved.
    # This is a simple heuristic; full compliance requires running
    # check_compliance against actual CoT entries.
    compliance_score: float | None = None
    if policy_count > 0:
        critical = severity_counts.get("critical", 0)
        high = severity_counts.get("high", 0)
        # Score decreases with more critical/high severity policies
        if critical + high == 0:
            compliance_score = 1.0
        else:
            compliance_score = round(
                max(0.0, 1.0 - (critical * 0.15 + high * 0.05)), 2
            )

    # Regulation count = number of distinct jurisdictions
    regulation_count = len(jurisdictions)

    return NomusIntegrationStatusResponse(
        configured=configured,
        connected=connected,
        nomus_url=status_data.get("nomus_url"),
        last_sync=status_data.get("last_sync"),
        last_error=status_data.get("last_error"),
        regulation_count=regulation_count,
        policy_count=policy_count,
        compliance_score=compliance_score,
        jurisdictions=jurisdictions,
        categories=status_data.get("categories", []),
        severity_counts=severity_counts,
        auto_sync=status_data.get("auto_sync", True),
        sync_interval_seconds=status_data.get("sync_interval_seconds", 21600),
        sync_count=status_data.get("sync_count", 0),
        version=status_data.get("version"),
        state_hash=status_data.get("state_hash"),
    )


@nomus_integration_router.get(
    "/policies",
    response_model=NomusPoliciesResponse,
)
async def nomus_integration_policies(
    identity: Identity = Depends(get_identity),
) -> NomusPoliciesResponse:
    """Return regulatory policies that Nomus has identified as applicable.

    These are the locally cached policies from the last Nomus sync.
    No data is sent to Nomus — this reads from the in-memory cache only.
    """
    policies = get_cached_policies()
    state_hash = get_cached_state_hash()

    summaries = [
        NomusPolicySummary(
            rule_key=p.get("ruleKey", ""),
            jurisdiction=p.get("jurisdiction", ""),
            category=p.get("category", ""),
            severity=p.get("severity", "medium"),
            effect=p.get("effect", "flag"),
            human_summary=p.get("humanSummary", ""),
            legal_reference=p.get("legalReference", ""),
        )
        for p in policies
    ]

    # Determine version from cached status
    from orchestrator.core.nomus_client import _bundle_version

    return NomusPoliciesResponse(
        total=len(summaries),
        version=_bundle_version,
        state_hash=state_hash,
        policies=summaries,
        issued_at=datetime.now(timezone.utc).isoformat(),
    )
