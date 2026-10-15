"""
Modus Conductor — Health Check Endpoints
================================================
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, text

from conductor.core.config import settings
from conductor.db.models import OrchestratorNode, ReconciliationSnapshot
from conductor.db.session import get_session_ctx

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok", "service": "conductor", "version": settings.version}


@router.get("/ready", tags=["health"])
async def readiness():
    """Readiness probe: 200 when the database answers, 503 otherwise.

    Same contract as the Orchestrator's /ready, so a load balancer or
    Kubernetes readiness probe takes an unready Conductor out of rotation.
    """
    try:
        async with get_session_ctx() as db:
            await db.execute(text("SELECT 1"))

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
    except Exception as exc:
        logger.warning("Readiness check: database unavailable", exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "conductor",
                "version": settings.version,
                "reason": "database_unavailable",
            },
        )

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
