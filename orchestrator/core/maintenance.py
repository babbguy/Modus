"""
Modus — Maintenance Tasks
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib import request as urllib_request
from urllib.error import URLError

from sqlalchemy import delete, select

from orchestrator.core.config import settings
from orchestrator.db.models import AgentHeartbeat, IngestBatch
from orchestrator.db.session import _session_factory

logger = logging.getLogger(__name__)

BATCH_DELETE_SIZE = 5000


async def _batched_delete(model, filter_col, cutoff, pk_col, *, label: str) -> int:
    """Delete rows in batches to avoid long-running transactions and lock contention."""
    if _session_factory is None:
        return 0
    total_deleted = 0
    while True:
        async with _session_factory() as db:
            pks = (await db.execute(
                select(pk_col).where(filter_col < cutoff).limit(BATCH_DELETE_SIZE)
            )).scalars().all()
            if not pks:
                break
            result = await db.execute(
                delete(model).where(pk_col.in_(pks))
            )
            await db.commit()
            total_deleted += result.rowcount
    logger.info("Pruned %s", label, extra={"deleted": total_deleted})
    return total_deleted


async def prune_heartbeats() -> None:
    """Delete heartbeats older than prune_heartbeats_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.prune_heartbeats_days)
    await _batched_delete(
        AgentHeartbeat, AgentHeartbeat.received_at, cutoff,
        AgentHeartbeat.id, label="heartbeats",
    )


async def prune_batch_ids() -> None:
    """Delete ingest batch dedup records older than prune_batches_hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.prune_batches_hours)
    await _batched_delete(
        IngestBatch, IngestBatch.received_at, cutoff,
        IngestBatch.batch_id, label="batch IDs",
    )


async def prune_real_time_spend() -> None:
    """Delete real_time_spend rows older than prune_real_time_spend_days."""
    from orchestrator.db.models import RealTimeSpend
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.prune_real_time_spend_days)
    await _batched_delete(
        RealTimeSpend, RealTimeSpend.window_end, cutoff,
        RealTimeSpend.id, label="real-time spend rows",
    )


async def ensure_partitions() -> None:
    """
    Create monthly partitions for usage_records (PostgreSQL only, Phase 4d).

    Declarative range partitioning by timestamp. Creates the current month
    and next 2 months' partitions if they don't exist. Idempotent — safe to
    run every maintenance cycle.

    No-op on SQLite or when partition_usage_records=False.
    """
    if _session_factory is None or settings.is_sqlite or not settings.partition_usage_records:
        return

    from sqlalchemy import text as sa_text

    now = datetime.now(timezone.utc)

    async with _session_factory() as db:
        for month_offset in range(0, 3):
            # Target month
            year = now.year + (now.month + month_offset - 1) // 12
            month = (now.month + month_offset - 1) % 12 + 1
            next_year = year + (month // 12)
            next_month = month % 12 + 1

            partition_name = f"usage_records_y{year}m{month:02d}"
            start_date = f"{year}-{month:02d}-01"
            end_date = f"{next_year}-{next_month:02d}-01"

            # Validate partition name format (defense-in-depth)
            import re
            if not re.fullmatch(r"usage_records_y\d{4}m\d{2}", partition_name):
                logger.error("Invalid partition name: %s", partition_name)
                continue

            # Validate date strings are strictly YYYY-MM-DD before interpolation
            _date_re = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
            if not _date_re.fullmatch(start_date) or not _date_re.fullmatch(end_date):
                logger.error("Invalid date format: start=%s end=%s", start_date, end_date)
                continue

            try:
                await db.execute(sa_text(f"""
                    CREATE TABLE IF NOT EXISTS {partition_name}
                    PARTITION OF usage_records
                    FOR VALUES FROM ('{start_date}') TO ('{end_date}')
                """))
                await db.commit()
            except Exception as exc:
                await db.rollback()
                # Partition may already exist or table isn't partitioned (SQLite)
                logger.debug("Partition %s skipped: %s", partition_name, exc)

        logger.debug("Partition check complete (next 3 months ensured)")


async def check_ingest_staleness() -> None:
    """
    Alert when no ingest records received for longer than heartbeat_stale_minutes.

    Prevents silent observability gaps — if agents stop reporting, this task
    logs a warning so monitoring systems (Prometheus, PagerDuty, Slack) can
    pick it up. Runs on the same schedule as other maintenance tasks.
    """
    if _session_factory is None:
        return
    from orchestrator.db.models import UsageRecord
    from sqlalchemy import select, func

    stale_minutes = settings.heartbeat_stale_minutes
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)

    async with _session_factory() as db:
        result = await db.execute(
            select(func.count(UsageRecord.id)).where(
                UsageRecord.ingested_at >= cutoff
            )
        )
        recent_count = result.scalar() or 0

        if recent_count == 0:
            logger.warning(
                "Ingest staleness alert: no usage records received in last %d minutes. "
                "Check that agents are running and network connectivity to the orchestrator.",
                stale_minutes,
                extra={"stale_minutes": stale_minutes, "recent_count": 0},
            )
        else:
            logger.debug(
                "Ingest health OK: %d records in last %d minutes",
                recent_count, stale_minutes,
            )


async def auto_reset_budget_suspensions() -> None:
    """
    Lift budget_suspended enforcement state when the app's period has reset.

    An app suspended for a daily budget breach should automatically become
    active again at the start of the next day. This task handles that reset
    so ops teams don't have to manually intervene every morning.

    Only resets budget_suspended — admin_suspended requires human action.
    """
    if _session_factory is None:
        return
    from orchestrator.db.models import App
    from sqlalchemy import select

    now = datetime.now(timezone.utc)

    async with _session_factory() as db:
        # Find apps that were suspended (budget_suspended) and whose
        # suspension is from a previous calendar day (daily period reset)
        result = await db.execute(
            select(App).where(
                App.enforcement_state == "budget_suspended",
                App.enforcement_suspended_at.isnot(None),
                App.enforcement_suspended_at < now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
                App.is_active == True,
            )
        )
        apps = result.scalars().all()

        for app in apps:
            app.enforcement_state = "active"
            app.enforcement_suspended_reason = None
            app.enforcement_suspended_at = None
            logger.info(
                "Auto-reset budget suspension",
                extra={"app_id": app.app_id, "team_id": str(app.team_id)},
            )

        if apps:
            await db.commit()
            logger.info(
                "Budget suspension auto-reset complete",
                extra={"count": len(apps)},
            )


# ── Auto-pause callback (Phase 4e) ───────────────────────────────────────────

async def notify_app_pause(app_id: str, app_name: str, pause_url: str, reason: str) -> bool:
    """
    Call an app's pause endpoint to request it stop making AI calls.

    POST to the app's registered pause_endpoint_url with a JSON payload
    containing the reason for the pause. Returns True on success, False on failure.
    Non-blocking — fires in a background thread.
    """
    import asyncio

    def _do_pause() -> bool:
        try:
            payload = json.dumps({
                "action": "pause",
                "app_id": app_id,
                "app_name": app_name,
                "reason": reason,
                "source": "modus",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }).encode()
            req = urllib_request.Request(
                pause_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Modus/2.0"},
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=5) as resp:
                return resp.status < 400
        except (URLError, Exception) as exc:
            logger.warning(
                "Auto-pause callback failed for %s: %s",
                app_name, exc,
            )
            return False

    try:
        result = await asyncio.get_running_loop().run_in_executor(None, _do_pause)
        if result:
            logger.info("Auto-pause callback succeeded", extra={"app_id": app_id, "url": pause_url})
        return result
    except Exception:
        return False


# ── Incident ticket auto-creation (Phase 4e) ─────────────────────────────────

async def create_incident_ticket(
    webhook_url: str,
    alert_data: dict,
) -> bool:
    """
    POST alert data to a webhook URL for incident ticket creation.

    Supports Jira, ServiceNow, Linear, or any generic webhook that accepts
    JSON POSTs. Configured via the threshold's notify JSONB field:

        {"incident_webhook": "https://jira.company.com/rest/api/2/issue",
         "incident_project": "OPS",
         "incident_priority": "P2"}

    Returns True on successful delivery.
    """
    import asyncio

    def _do_post() -> bool:
        try:
            payload = json.dumps({
                "source": "modus",
                "event_type": "threshold_breach",
                "severity": alert_data.get("severity", "critical"),
                "app_id": alert_data.get("app_id"),
                "app_name": alert_data.get("app_name"),
                "team_id": alert_data.get("team_id"),
                "metric": alert_data.get("metric"),
                "actual_value": alert_data.get("actual_value"),
                "threshold_value": alert_data.get("threshold_value"),
                "description": (
                    f"[Modus] {alert_data.get('severity', 'CRITICAL').upper()} threshold breach: "
                    f"{alert_data.get('app_name', 'unknown')} — "
                    f"{alert_data.get('metric', 'cost')} = {alert_data.get('actual_value', '?')} "
                    f"(threshold: {alert_data.get('threshold_value', '?')})"
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }).encode()
            req = urllib_request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Modus/2.0"},
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=10) as resp:
                return resp.status < 400
        except (URLError, Exception) as exc:
            logger.warning("Incident ticket creation failed: %s", exc)
            return False

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _do_post)
    except Exception:
        return False


# ── Storage Compaction (Scale Pack foundation) ────────────────────────────────
#
# Rolls up old raw UsageRecord rows into hourly UsageAggregate summaries,
# then deletes the raw rows. This keeps the database bounded regardless of
# call volume — the DB only ever holds recent raw data + compact aggregates.
#
# Run schedule: hourly (via scheduler in main.py)
# Compaction window: records older than compaction_after_hours (default: 24)
# Batch size: processes 10,000 records at a time to avoid long transactions.


async def compact_usage_records() -> None:
    """
    Roll up raw usage_records older than compaction_after_hours into
    hourly usage_aggregates, then delete the raw rows.

    This is the foundation of bounded storage at scale:
    - 1B raw calls → ~24h of raw data + compact hourly/daily aggregates
    - DB size stays predictable regardless of throughput
    """
    if _session_factory is None:
        return

    compaction_hours = getattr(settings, "compaction_after_hours", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=compaction_hours)
    batch_size = 10000

    from orchestrator.db.models import UsageRecord

    total_compacted = 0
    total_deleted = 0

    while True:
        async with _session_factory() as db:
            # Fetch a batch of old records grouped by the compaction key
            # (app_id, team_id, provider, model, resource_type, hour)
            #
            # We use a subquery to find the oldest batch of record IDs,
            # then aggregate them in Python for portability (SQLite + PG).
            old_ids_q = (
                select(UsageRecord.id)
                .where(UsageRecord.timestamp < cutoff)
                .limit(batch_size)
            )
            old_ids = (await db.execute(old_ids_q)).scalars().all()

            if not old_ids:
                break

            # Fetch the actual records for aggregation
            records_q = select(UsageRecord).where(UsageRecord.id.in_(old_ids))
            records = (await db.execute(records_q)).scalars().all()

            if not records:
                break

            # Group by (app_id, team_id, provider, model, resource_type, hour)
            buckets: dict[tuple, dict] = {}
            for rec in records:
                hour_start = rec.timestamp.replace(minute=0, second=0, microsecond=0)
                key = (
                    rec.app_id, rec.team_id, rec.provider,
                    rec.model, rec.resource_type, hour_start,
                )
                if key not in buckets:
                    buckets[key] = {
                        "call_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "input_cost": 0,
                        "output_cost": 0,
                        "total_cost": 0,
                        "duration_ms_sum": 0,
                        "duration_ms_min": None,
                        "duration_ms_max": None,
                    }
                b = buckets[key]
                b["call_count"] += 1
                b["input_tokens"] += rec.input_tokens or 0
                b["output_tokens"] += rec.output_tokens or 0
                b["total_tokens"] += rec.total_tokens or 0
                b["input_cost"] += rec.input_cost or 0
                b["output_cost"] += rec.output_cost or 0
                b["total_cost"] += rec.total_cost or 0
                if rec.duration_ms is not None:
                    b["duration_ms_sum"] += rec.duration_ms
                    if b["duration_ms_min"] is None or rec.duration_ms < b["duration_ms_min"]:
                        b["duration_ms_min"] = rec.duration_ms
                    if b["duration_ms_max"] is None or rec.duration_ms > b["duration_ms_max"]:
                        b["duration_ms_max"] = rec.duration_ms

            # Upsert aggregates via the write queue
            from orchestrator.core.write_queue import enqueue, AggregationItem
            import uuid as _uuid_mod

            agg_rows = []
            for (app_id, team_id, provider, model, resource_type, hour_start), b in buckets.items():
                avg_dur = b["duration_ms_sum"] // b["call_count"] if b["call_count"] > 0 else None
                agg_rows.append(dict(
                    id=str(_uuid_mod.uuid4()),
                    app_id=app_id,
                    team_id=team_id,
                    provider=provider,
                    model=model,
                    resource_type=resource_type,
                    granularity="hourly",
                    period_start=hour_start,
                    period_end=hour_start + timedelta(hours=1),
                    call_count=b["call_count"],
                    input_tokens=b["input_tokens"],
                    output_tokens=b["output_tokens"],
                    total_tokens=b["total_tokens"],
                    input_cost=b["input_cost"],
                    output_cost=b["output_cost"],
                    total_cost=b["total_cost"],
                    avg_duration_ms=avg_dur,
                    min_duration_ms=b["duration_ms_min"],
                    max_duration_ms=b["duration_ms_max"],
                    duration_ms_sum=b["duration_ms_sum"],
                    source="compaction",
                ))

            if agg_rows:
                await enqueue(AggregationItem(rows=agg_rows, granularity="hourly"))

            # Delete the compacted raw records
            await db.execute(
                delete(UsageRecord).where(UsageRecord.id.in_(old_ids))
            )
            await db.commit()

            total_compacted += len(records)
            total_deleted += len(old_ids)

    if total_compacted > 0:
        logger.info(
            "Storage compaction complete",
            extra={
                "records_compacted": total_compacted,
                "records_deleted": total_deleted,
                "cutoff_hours": compaction_hours,
            },
        )


async def compact_hourly_to_daily() -> None:
    """
    Roll up hourly aggregates older than 7 days into daily aggregates.

    Second stage of the compaction pipeline:
    - Hot: raw records (0-24h)
    - Warm: hourly aggregates (1-7d)
    - Cold: daily aggregates (7d+)
    """
    if _session_factory is None:
        return

    from orchestrator.db.models import UsageAggregate
    import uuid as _uuid_mod

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    async with _session_factory() as db:
        # Find hourly aggregates older than 7 days
        hourly_q = (
            select(UsageAggregate)
            .where(
                UsageAggregate.granularity == "hourly",
                UsageAggregate.period_start < cutoff,
            )
            .limit(5000)
        )
        hourlies = (await db.execute(hourly_q)).scalars().all()

        if not hourlies:
            return

        # Group by (app_id, team_id, provider, model, resource_type, date)
        daily_buckets: dict[tuple, dict] = {}
        hourly_ids = []

        for h in hourlies:
            hourly_ids.append(h.id)
            day_start = h.period_start.replace(hour=0, minute=0, second=0, microsecond=0)
            key = (h.app_id, h.team_id, h.provider, h.model, h.resource_type, day_start)

            if key not in daily_buckets:
                daily_buckets[key] = {
                    "call_count": 0, "input_tokens": 0, "output_tokens": 0,
                    "total_tokens": 0, "input_cost": 0, "output_cost": 0,
                    "total_cost": 0, "duration_ms_sum": 0,
                    "duration_ms_min": None, "duration_ms_max": None,
                }
            b = daily_buckets[key]
            b["call_count"] += h.call_count
            b["input_tokens"] += h.input_tokens
            b["output_tokens"] += h.output_tokens
            b["total_tokens"] += h.total_tokens
            b["input_cost"] += h.input_cost or 0
            b["output_cost"] += h.output_cost or 0
            b["total_cost"] += h.total_cost
            b["duration_ms_sum"] += h.duration_ms_sum or 0
            if h.min_duration_ms is not None:
                if b["duration_ms_min"] is None or h.min_duration_ms < b["duration_ms_min"]:
                    b["duration_ms_min"] = h.min_duration_ms
            if h.max_duration_ms is not None:
                if b["duration_ms_max"] is None or h.max_duration_ms > b["duration_ms_max"]:
                    b["duration_ms_max"] = h.max_duration_ms

        # Upsert daily aggregates
        from orchestrator.core.write_queue import enqueue, AggregationItem

        agg_rows = []
        for (app_id, team_id, provider, model, resource_type, day_start), b in daily_buckets.items():
            avg_dur = b["duration_ms_sum"] // b["call_count"] if b["call_count"] > 0 else None
            agg_rows.append(dict(
                id=str(_uuid_mod.uuid4()),
                app_id=app_id,
                team_id=team_id,
                provider=provider,
                model=model,
                resource_type=resource_type,
                granularity="daily",
                period_start=day_start,
                period_end=day_start + timedelta(days=1),
                call_count=b["call_count"],
                input_tokens=b["input_tokens"],
                output_tokens=b["output_tokens"],
                total_tokens=b["total_tokens"],
                input_cost=b["input_cost"],
                output_cost=b["output_cost"],
                total_cost=b["total_cost"],
                avg_duration_ms=avg_dur,
                min_duration_ms=b["duration_ms_min"],
                max_duration_ms=b["duration_ms_max"],
                duration_ms_sum=b["duration_ms_sum"],
                source="compaction",
            ))

        if agg_rows:
            await enqueue(AggregationItem(rows=agg_rows, granularity="daily"))

        # Delete the compacted hourly aggregates
        if hourly_ids:
            await db.execute(
                delete(UsageAggregate).where(UsageAggregate.id.in_(hourly_ids))
            )
            await db.commit()

        logger.info(
            "Hourly→daily compaction complete",
            extra={"hourlies_compacted": len(hourly_ids), "daily_buckets": len(daily_buckets)},
        )
