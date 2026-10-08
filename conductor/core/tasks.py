"""
Modus Conductor — Background Tasks
==========================================
Periodic background tasks for data reconciliation and maintenance.
"""

from __future__ import annotations

import asyncio
import logging

from conductor.core.config import settings

logger = logging.getLogger(__name__)


async def _run_reconciliation_loop() -> None:
    """Periodic reconciliation — validates data completeness."""
    interval = settings.reconciliation_interval_seconds
    logger.info("Reconciliation loop started (interval=%ds)", interval)

    # Wait one cycle before first run to let Orchestrators register
    await asyncio.sleep(min(interval, 30))

    while True:
        try:
            from conductor.core.reconciler import run_reconciliation
            from conductor.db.session import get_session_ctx

            async with get_session_ctx() as db:
                await run_reconciliation(db)
        except asyncio.CancelledError:
            logger.info("Reconciliation loop cancelled")
            return
        except Exception:
            logger.exception("Reconciliation loop error")

        await asyncio.sleep(interval)


async def _run_maintenance_loop() -> None:
    """Periodic maintenance — prune old push receipts, reconciliation snapshots."""
    interval = 3600  # hourly
    logger.info("Maintenance loop started (interval=%ds)", interval)

    await asyncio.sleep(interval)

    while True:
        try:
            from datetime import datetime, timedelta, timezone
            from sqlalchemy import delete
            from conductor.db.models import PushReceipt, ReconciliationSnapshot
            from conductor.db.session import get_session_ctx

            cutoff_receipts = datetime.now(timezone.utc) - timedelta(hours=48)
            cutoff_recon = datetime.now(timezone.utc) - timedelta(days=7)

            async with get_session_ctx() as db:
                # Prune old push receipts (keep 48h)
                result = await db.execute(
                    delete(PushReceipt).where(PushReceipt.received_at < cutoff_receipts)
                )
                receipts_pruned = result.rowcount

                # Prune old reconciliation snapshots (keep 7d)
                result = await db.execute(
                    delete(ReconciliationSnapshot).where(
                        ReconciliationSnapshot.snapshot_at < cutoff_recon
                    )
                )
                recon_pruned = result.rowcount

                if receipts_pruned or recon_pruned:
                    logger.info(
                        "Maintenance: pruned %d receipts, %d reconciliation snapshots",
                        receipts_pruned, recon_pruned,
                    )
        except asyncio.CancelledError:
            logger.info("Maintenance loop cancelled")
            return
        except Exception:
            logger.exception("Maintenance loop error")

        await asyncio.sleep(interval)
