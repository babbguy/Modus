"""
Modus — Aggregation Engine
====================================
Computes hourly and daily rollups from raw usage_records into usage_aggregates.

Called by the background task in main.py every aggregate_interval_seconds.

Design: upsert-based — safe to run multiple times over the same window.
Computes the last 2 hours (hourly) and last 2 days (daily) on each run.
The 2x window ensures records that arrive late are captured on the next cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from orchestrator.db.models import UsageRecord
from orchestrator.db.session import _session_factory
from orchestrator.core.config import settings
from orchestrator.metrics.prometheus import AGGREGATE_DURATION_SECONDS, AGGREGATE_RUNS_TOTAL

import time

logger = logging.getLogger(__name__)


def _trunc_expr(granularity: str, col):
    """Return a date truncation expression that works on both PostgreSQL and SQLite."""
    # Check both settings and the actual engine to avoid misdetection
    from orchestrator.db.session import _engine
    is_sqlite = settings.is_sqlite or (
        _engine is not None and "sqlite" in _engine.url.drivername
    )
    if is_sqlite:
        if granularity == "hour":
            return func.strftime("%Y-%m-%d %H:00:00", col)
        else:  # day
            return func.strftime("%Y-%m-%d 00:00:00", col)
    else:
        from sqlalchemy import func as sqla_func
        return sqla_func.date_trunc(granularity, col)


async def run_aggregation_cycle() -> None:
    """Run one aggregation cycle — hourly and daily rollups.

    Reads are done in a read-only session. Writes are pushed through the
    write queue so they never compete with ingest/heartbeat for the SQLite lock.
    """
    start = time.perf_counter()
    try:
        if _session_factory is None:
            return

        from orchestrator.core.write_queue import enqueue, AggregationItem

        async with _session_factory() as db:
            now = datetime.now(timezone.utc)

            hourly_rows = await _compute_aggregates(db, now, "hourly")
            daily_rows = await _compute_aggregates(db, now, "daily")
            # Read-only session — no commit needed

        # Push writes through the write queue (serialized with all other writes)
        if hourly_rows:
            await enqueue(AggregationItem(rows=hourly_rows, granularity="hourly"))
        if daily_rows:
            await enqueue(AggregationItem(rows=daily_rows, granularity="daily"))

        duration = time.perf_counter() - start
        AGGREGATE_RUNS_TOTAL.labels(status="success").inc()
        AGGREGATE_DURATION_SECONDS.observe(duration)
        logger.debug("Aggregation cycle complete", extra={
            "duration_ms": round(duration * 1000),
            "hourly_rows": len(hourly_rows),
            "daily_rows": len(daily_rows),
        })

    except Exception as exc:
        AGGREGATE_RUNS_TOTAL.labels(status="error").inc()
        logger.error("Aggregation cycle error", exc_info=exc)
        raise


async def _compute_aggregates(db, now: datetime, granularity: str) -> list[dict]:
    """Read raw usage records and return pre-computed aggregate dicts.

    Returns a list of dicts ready for UsageAggregate upsert. Does NOT write
    to the database — the caller pushes these through the write queue.
    """
    if granularity == "hourly":
        window_start = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        period_delta = timedelta(hours=1)
        trunc_gran = "hour"
    else:
        window_start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        period_delta = timedelta(days=1)
        trunc_gran = "day"

    trunc_col = _trunc_expr(trunc_gran, UsageRecord.timestamp)

    result = await db.execute(
        select(
            UsageRecord.app_id,
            UsageRecord.team_id,
            UsageRecord.provider,
            UsageRecord.model,
            UsageRecord.resource_type,
            trunc_col.label("period_start"),
            func.count(UsageRecord.id).label("call_count"),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(UsageRecord.total_cost), 0).label("total_cost"),
            func.avg(UsageRecord.duration_ms).label("avg_duration_ms"),
        ).where(
            UsageRecord.timestamp >= window_start
        ).group_by(
            UsageRecord.app_id,
            UsageRecord.team_id,
            UsageRecord.provider,
            UsageRecord.model,
            UsageRecord.resource_type,
            trunc_col,
        )
    )

    rows = []
    for row in result.all():
        period_start = row.period_start
        if isinstance(period_start, str):
            period_start = datetime.strptime(period_start, "%Y-%m-%d %H:%M:%S")
        if period_start.tzinfo is None:
            period_start = period_start.replace(tzinfo=timezone.utc)

        rows.append(dict(
            app_id=str(row.app_id),
            team_id=str(row.team_id),
            provider=row.provider,
            model=row.model,
            resource_type=row.resource_type,
            granularity=granularity,
            period_start=period_start,
            period_end=period_start + period_delta,
            call_count=row.call_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            total_cost=row.total_cost,
            avg_duration_ms=int(row.avg_duration_ms) if row.avg_duration_ms else None,
        ))
    return rows
