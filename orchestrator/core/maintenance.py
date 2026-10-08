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


# ── Storage retention (Scale Pack foundation) ─────────────────────────────────
#
# Usage is counted into the hourly AND daily aggregates at ingest time (see
# orchestrator/core/usage_rollup.py), so retention never has to roll anything
# up: it only deletes detail that the aggregates already contain.
#
#   Hot:  raw usage_records       0 .. compaction_after_hours (default 24h)
#   Warm: hourly aggregates       0 .. hourly_aggregate_retention_days (7d)
#   Cold: daily aggregates        forever
#
# Run schedule: hourly (via the maintenance loop in tasks.py).


async def compact_usage_records() -> None:
    """Delete raw usage_records older than ``compaction_after_hours``.

    They were counted into the aggregates when they were ingested, so deleting
    them does not change any total. (Earlier versions rolled them up again
    here, which double counted.)
    """
    if _session_factory is None:
        return
    from orchestrator.db.models import UsageRecord

    compaction_hours = getattr(settings, "compaction_after_hours", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=compaction_hours)
    deleted = await _batched_delete(
        UsageRecord, UsageRecord.timestamp, cutoff, UsageRecord.id,
        label="raw usage records",
    )
    if deleted:
        logger.info(
            "Raw usage record retention complete",
            extra={"records_deleted": deleted, "cutoff_hours": compaction_hours},
        )


async def compact_hourly_to_daily() -> None:
    """Prune hourly aggregates older than ``hourly_aggregate_retention_days``.

    The daily rows already hold the same totals (both granularities are
    updated in the same ingest transaction). Before a day's hourly rows are
    deleted, any key that has hourly rows but no daily row at all is filled
    in from the hourly rows, so even data written by older versions is never
    lost. Existing daily rows are never replaced here. One day per
    transaction.
    """
    if _session_factory is None:
        return

    from orchestrator.core.usage_rollup import day_floor, oldest_hourly_day, reconcile_daily

    retention_days = getattr(settings, "hourly_aggregate_retention_days", 7)
    cutoff_day = day_floor(datetime.now(timezone.utc)) - timedelta(days=retention_days)

    pruned = 0
    days = 0
    filled = 0
    while True:
        async with _session_factory() as db:
            day = await oldest_hourly_day(db, cutoff_day)
            if day is None:
                break
            res = await reconcile_daily(db, [day], prune_hourly=True)
            await db.commit()
        days += 1
        pruned += res.hourly_rows_pruned
        filled += res.keys_repaired
        if res.hourly_rows_pruned == 0:
            # Defensive: never spin if a delete removed nothing.
            break

    if days:
        logger.info(
            "Hourly aggregate retention complete",
            extra={"days": days, "hourly_rows_pruned": pruned, "daily_keys_filled": filled},
        )
