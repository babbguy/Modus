"""
Modus Conductor — Data Reconciliation Engine
===================================================
Runs periodically to validate data completeness and integrity.

Responsibilities:
  1. Check which Orchestrators have pushed data recently
  2. Calculate completeness percentage
  3. Flag stale Orchestrators
  4. Detect discrepancies in aggregated totals
  5. Store reconciliation snapshots for audit trail

Bank-grade requirement: the dashboard must never silently show partial data
as if it were complete. If data is incomplete, the dashboard must know.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.core.cache import CacheTier, cache
from conductor.core.config import settings
from conductor.db.models import (
    ConductorAggregate,
    OrchestratorNode,
    ReconciliationSnapshot,
)

logger = logging.getLogger(__name__)


async def run_reconciliation(db: AsyncSession) -> ReconciliationSnapshot:
    """
    Execute one reconciliation cycle.

    Returns a ReconciliationSnapshot with the current state.
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=settings.orchestrator_stale_minutes)

    # 1. Count active Orchestrators
    total_result = await db.execute(
        select(func.count()).select_from(OrchestratorNode).where(
            OrchestratorNode.is_active == True  # noqa: E712
        )
    )
    total_orchestrators = total_result.scalar() or 0

    # 2. Count Orchestrators that pushed data recently
    reporting_result = await db.execute(
        select(func.count()).select_from(OrchestratorNode).where(
            OrchestratorNode.is_active == True,  # noqa: E712
            OrchestratorNode.last_push_at >= stale_cutoff,
        )
    )
    reporting_orchestrators = reporting_result.scalar() or 0

    # 3. Calculate completeness
    if total_orchestrators == 0:
        completeness_pct = 100.0  # No orchestrators = nothing to be incomplete about
    else:
        completeness_pct = round(
            (reporting_orchestrators / total_orchestrators) * 100, 2
        )

    # 4. Flag stale Orchestrators
    stale_result = await db.execute(
        select(OrchestratorNode).where(
            OrchestratorNode.is_active == True,  # noqa: E712
            OrchestratorNode.last_push_at < stale_cutoff,
        )
    )
    stale_nodes = stale_result.scalars().all()

    discrepancies = []
    for node in stale_nodes:
        await db.execute(
            update(OrchestratorNode)
            .where(OrchestratorNode.id == node.id)
            .values(consecutive_missed_pushes=node.consecutive_missed_pushes + 1)
        )
        discrepancies.append({
            "orchestrator_id": node.id,
            "orchestrator_name": node.name,
            "instance_id": node.instance_id,
            "last_push_at": node.last_push_at.isoformat() if node.last_push_at else None,
            "missed_pushes": node.consecutive_missed_pushes + 1,
            "type": "stale_orchestrator",
        })

    # 5. Calculate org-wide MTD totals
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    cost_result = await db.execute(
        select(
            func.coalesce(func.sum(ConductorAggregate.total_cost), 0),
            func.coalesce(func.sum(ConductorAggregate.call_count), 0),
        ).where(
            ConductorAggregate.granularity == "daily",
            ConductorAggregate.period_start >= month_start,
        )
    )
    row = cost_result.one()
    total_cost_mtd = row[0]
    total_calls_mtd = row[1]

    # 6. Determine status
    if completeness_pct >= settings.completeness_threshold_pct:
        status = "complete"
    elif completeness_pct >= 50.0:
        status = "partial"
    else:
        status = "degraded"

    # 7. Create snapshot
    snapshot = ReconciliationSnapshot(
        total_orchestrators=total_orchestrators,
        reporting_orchestrators=reporting_orchestrators,
        completeness_pct=completeness_pct,
        total_cost_mtd=total_cost_mtd,
        total_calls_mtd=total_calls_mtd,
        discrepancies=discrepancies if discrepancies else None,
        status=status,
    )
    db.add(snapshot)

    # 8. Invalidate hot cache when completeness changes —
    #    forces dashboard to get fresh numbers on next load
    await cache.invalidate_tier(CacheTier.HOT)

    logger.info(
        "Reconciliation complete: %d/%d orchestrators reporting (%.1f%%), status=%s, MTD=$%.2f",
        reporting_orchestrators,
        total_orchestrators,
        completeness_pct,
        status,
        float(total_cost_mtd),
    )

    return snapshot
