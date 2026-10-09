"""
Modus — Aggregation Engine
====================================
Periodic reconciliation of the usage aggregates.

Usage is counted into ``usage_aggregates`` exactly once, at ingest: the
write-queue writer adds each batch to its hourly and daily rows in the same
transaction that claims the batch id (see orchestrator/core/usage_rollup.py
for the source-of-truth rules). This task therefore never re-reads raw
``usage_records`` and never adds anything.

Every ``aggregate_interval_seconds`` it recomputes the daily rows of today and
yesterday (UTC) from their hourly rows and REPLACES any daily row that
differs. With correct data this is a no-op, so running it any number of times
never changes a total; it only repairs drift (for example rows edited by
hand) and logs a warning when it does.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from orchestrator.db import session as _session_mod
from orchestrator.metrics.prometheus import AGGREGATE_DURATION_SECONDS, AGGREGATE_RUNS_TOTAL

logger = logging.getLogger(__name__)

# Days (UTC, counting today) whose daily rows are reconciled each cycle. Must
# stay inside the hourly retention window (hourly_aggregate_retention_days,
# minimum 2), where the hourly rows of a day are complete.
RECONCILE_DAYS = 2


def reconcile_days(now: datetime) -> list[datetime]:
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return [today - timedelta(days=i) for i in range(RECONCILE_DAYS)]


async def run_aggregation_cycle() -> None:
    """Queue one reconciliation of today's and yesterday's daily aggregates.

    The work runs in the write-queue writer, serialized with ingest, so it
    never competes with ingest for the SQLite lock.
    """
    start = time.perf_counter()
    try:
        if _session_mod._session_factory is None:
            return

        from orchestrator.core.write_queue import ReconcileItem, enqueue

        queued = await enqueue(ReconcileItem(days=reconcile_days(datetime.now(timezone.utc))))
        if not queued:
            logger.warning("Aggregate reconciliation skipped: write queue full")

        duration = time.perf_counter() - start
        AGGREGATE_RUNS_TOTAL.labels(status="success" if queued else "skipped").inc()
        AGGREGATE_DURATION_SECONDS.observe(duration)

    except Exception as exc:
        AGGREGATE_RUNS_TOTAL.labels(status="error").inc()
        logger.error("Aggregation cycle error", exc_info=exc)
        raise
