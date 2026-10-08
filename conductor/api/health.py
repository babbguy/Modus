"""
Modus Conductor — Health Check Endpoints
================================================
"""

from __future__ import annotations


from fastapi import APIRouter, Depends
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.core.config import settings
from conductor.db.models import OrchestratorNode, ReconciliationSnapshot
from conductor.db.session import get_session

router = APIRouter()


@router.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok", "service": "conductor", "version": settings.version}


@router.get("/ready", tags=["health"])
async def readiness(db: AsyncSession = Depends(get_session)):
    """Readiness probe — checks DB connectivity and Orchestrator status."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        return {"status": "not_ready", "reason": "database_unavailable"}

    # Count active orchestrators
    result = await db.execute(
        select(func.count()).select_from(OrchestratorNode).where(
            OrchestratorNode.is_active == True  # noqa: E712
        )
    )
    orchestrator_count = result.scalar() or 0

    # Get latest reconciliation
    recon_result = await db.execute(
        select(ReconciliationSnapshot)
        .order_by(ReconciliationSnapshot.snapshot_at.desc())
        .limit(1)
    )
    latest_recon = recon_result.scalar_one_or_none()

    return {
        "status": "ready",
        "service": "conductor",
        "version": settings.version,
        "region_id": settings.region_id or None,
        "orchestrators_registered": orchestrator_count,
        "data_completeness_pct": float(latest_recon.completeness_pct) if latest_recon else None,
        "data_status": latest_recon.status if latest_recon else "unknown",
        "last_reconciliation": latest_recon.snapshot_at.isoformat() if latest_recon else None,
    }
