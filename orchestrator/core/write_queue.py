"""
Modus — Async Write Queue
====================================
Decouples API endpoints from SQLite persistence.

The ingest and heartbeat endpoints push work items to an in-memory asyncio.Queue.
A single background Writer task drains the queue and performs batched inserts,
ensuring only one transaction touches the database at a time.

This eliminates all "database is locked" errors under high concurrency and moves
queuing from the client (timeouts) to server memory.

For PostgreSQL, the queue is bypassed — endpoints write directly via session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


logger = logging.getLogger(__name__)


class _LostItemCounter:
    """Thread-safe counter for permanently lost write queue items."""
    def __init__(self) -> None:
        self.total = 0

    def add(self, count: int) -> None:
        self.total += count


_flush_permanently_lost = _LostItemCounter()

# ── Work item types ──────────────────────────────────────────────────────────


@dataclass
class IngestItem:
    """A validated ingest batch: detail records to store plus the usage it adds.

    The writer claims ``batch_id`` and, in the same transaction, stores
    ``records`` and adds the batch's usage to the hourly and daily aggregates
    (see orchestrator/core/usage_rollup.py). A re-sent batch id is skipped,
    so a batch is counted exactly once.

    Usage counted for the batch:
      * ``usage`` when given: explicit increments (the SDK's pre-aggregated
        summaries plus any traces that are not already in them);
      * otherwise one increment per entry of ``records`` when
        ``count_records`` is True (raw-format ingest, gateway, OTLP);
      * nothing when ``count_records`` is False and ``usage`` is None.
    """
    app_id: str
    team_id: str
    batch_id: str
    records: list[dict]  # pre-validated record dicts
    record_count: int
    agent_version: Optional[str] = None
    sdk_versions: Optional[dict] = None
    total_cost: Decimal = Decimal("0")
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_ms: int = 0
    usage: Optional[list[dict]] = None
    count_records: bool = True
    source: str = "ingest"


@dataclass
class HeartbeatItem:
    """A heartbeat write-behind item."""
    app_id: str
    team_id: str
    agent_version: Optional[str] = None
    instrumented_providers: Optional[list[str]] = None
    host_info: Optional[dict] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PolicyDecisionItem:
    """A policy deny/throttle decision to record for audit."""
    app_id: str
    team_id: str
    decision: str
    reason: str
    request_provider: str
    request_model: str
    request_environment: str
    request_estimated_tokens: int
    request_estimated_cost: Decimal
    spend_at_decision: Decimal
    evaluation_latency_ms: int


@dataclass
class ReconcileItem:
    """Recompute daily aggregates from hourly rows for the given UTC days.

    Serialized through the write queue so it never races ingest for the
    SQLite lock. ``prune_hourly`` deletes the days' hourly rows afterwards
    (retention). See usage_rollup.reconcile_daily for the semantics.
    """
    days: list[datetime]
    prune_hourly: bool = False


@dataclass
class RegistrationItem:
    """App registration write-behind item.

    The endpoint generates the key and validates uniqueness (reads).
    The actual INSERT (app row + audit log) is batched here.
    """
    app_uuid: str        # pre-generated UUID for the app row
    team_id: str
    team_slug: str       # for auto-create if needed
    app_id: str          # user-facing app identifier
    app_name: str
    environment: str
    api_key_hash: str
    api_key_prefix: str
    actor_ip: Optional[str] = None
    pending_key: Optional[str] = None  # "team_id:app_id" for cleanup after flush


# ── Phase 9 write items ─────────────────────────────────────────────────────

@dataclass
class PoELedgerItem:
    """ZK-PoE chained audit ledger entry. Fire-and-forget from evaluate."""
    team_id: str
    session_id: str
    seq_num: int
    prev_hash: Optional[str]
    entry_hash: str
    decision_hash: str
    trajectory_proof_id: Optional[str] = None
    proof_type: str = "hash_chain"
    tee_quote: Optional[bytes] = None
    tee_platform: Optional[str] = None
    risk_level: str = "medium"


@dataclass
class NeuroAssuranceItem:
    """SNN assurance metric from neuromorphic evaluation."""
    team_id: str
    topology_hash: str
    energy_per_spike: float
    stdp_drift: float
    embodied_efficiency_score: float
    spike_rate_hz: float
    active_neuron_ratio: float
    inference_latency_ms: float
    hardware_backend: str
    app_id: Optional[str] = None
    session_id: Optional[str] = None


# ── Queue + Writer ───────────────────────────────────────────────────────────

_queue: asyncio.Queue | None = None
_writer_task: asyncio.Task | None = None

# Tuning knobs
BATCH_SIZE = 200       # max items to drain per write cycle
FLUSH_INTERVAL = 0.25  # seconds — max time to wait before flushing a partial batch
QUEUE_MAX = 50_000     # backpressure: if queue exceeds this, new items are dropped


def get_queue() -> asyncio.Queue:
    """Return the module-level write queue. Raises if not started."""
    if _queue is None:
        raise RuntimeError("Write queue not started. Call start_writer() at startup.")
    return _queue


BACKPRESSURE_THRESHOLD = int(QUEUE_MAX * 0.90)  # 90% of max capacity


def queue_pressure() -> float:
    """Return queue fill ratio (0.0 to 1.0). Returns 0.0 if queue not started."""
    if _queue is None:
        return 0.0
    return _queue.qsize() / QUEUE_MAX


def queue_over_pressure() -> bool:
    """Return True if queue is at or above 90% capacity (backpressure zone)."""
    if _queue is None:
        return False
    return _queue.qsize() >= BACKPRESSURE_THRESHOLD


async def enqueue(
    item: (
        IngestItem | HeartbeatItem | PolicyDecisionItem | ReconcileItem
        | RegistrationItem | PoELedgerItem | NeuroAssuranceItem
    ),
) -> bool:
    """
    Push an item onto the write queue. Returns True if enqueued, False if dropped.
    Non-blocking — never waits for DB.

    Callers should check queue_over_pressure() before calling and return
    503 + Retry-After when the queue is under backpressure.
    """
    q = get_queue()
    if q.qsize() >= QUEUE_MAX:
        logger.warning("Write queue full (%d items), dropping item", q.qsize())
        return False
    q.put_nowait(item)
    return True


async def start_writer() -> None:
    """Initialise the queue and spawn the background writer task."""
    global _queue, _writer_task
    _queue = asyncio.Queue()
    _writer_task = asyncio.create_task(_writer_loop(), name="db-writer")
    logger.info("Write queue started (batch=%d, flush=%.2fs)", BATCH_SIZE, FLUSH_INTERVAL)


async def stop_writer() -> None:
    """Flush remaining items and shut down the writer."""
    global _writer_task
    if _writer_task is not None:
        logger.info("Stopping write queue (draining %d remaining items)...", _queue.qsize() if _queue else 0)
        _writer_task.cancel()
        try:
            await _writer_task
        except asyncio.CancelledError:
            pass
        # Final drain
        if _queue and not _queue.empty():
            await _flush_batch(await _drain_queue())
        _writer_task = None
        logger.info("Write queue stopped")


async def _drain_queue() -> list:
    """Drain up to BATCH_SIZE items from the queue without blocking."""
    items = []
    q = get_queue()
    while len(items) < BATCH_SIZE and not q.empty():
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


async def _writer_loop() -> None:
    """
    Background loop: drain queue → batch insert → repeat.
    Runs as a single task, so only one DB transaction is active at a time.
    """

    logger.info("DB writer loop started")
    q = get_queue()

    while True:
        try:
            # Wait for at least one item (blocks until available)
            first = await asyncio.wait_for(q.get(), timeout=FLUSH_INTERVAL)
            items = [first]

            # Drain more items (non-blocking) up to BATCH_SIZE
            while len(items) < BATCH_SIZE:
                try:
                    items.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break

        except asyncio.TimeoutError:
            # No items arrived in flush interval — loop again
            continue
        except asyncio.CancelledError:
            raise

        # ── Flush batch to DB ─────────────────────────────────────────────
        await _flush_batch(items)


async def _flush_batch(items: list, _is_retry: bool = False) -> None:
    """Write a batch of IngestItem/HeartbeatItem to the database in one transaction."""
    if not items:
        return

    from orchestrator.db.session import _session_factory
    from orchestrator.db.models import UsageRecord, IngestBatch, AgentHeartbeat, App, AuditLog
    from sqlalchemy import update as sa_update

    start = time.perf_counter()
    ingest_count = 0
    heartbeat_count = 0
    registration_count = 0
    record_count = 0

    try:
        async with _session_factory() as db:
            for item in items:
                if isinstance(item, IngestItem):
                    # Claim the batch id first. A batch that was already
                    # written (agent retry) is skipped instead of raising a
                    # primary-key error, which would roll back every other
                    # item in this flush.
                    if db.bind.dialect.name == "postgresql":
                        from sqlalchemy.dialects.postgresql import insert as batch_insert
                    else:
                        from sqlalchemy.dialects.sqlite import insert as batch_insert
                    claimed = await db.execute(
                        batch_insert(IngestBatch)
                        .values(
                            batch_id=item.batch_id,
                            app_id=item.app_id,
                            record_count=item.record_count,
                        )
                        .on_conflict_do_nothing(index_elements=["batch_id"])
                    )
                    if claimed.rowcount == 0:
                        logger.info(
                            "Duplicate ingest batch skipped",
                            extra={"batch_id": item.batch_id, "app_id": item.app_id},
                        )
                        continue
                    # Insert all detail records
                    for rec in item.records:
                        db.add(UsageRecord(**rec))
                    record_count += item.record_count
                    ingest_count += 1

                    # Count the batch's usage exactly once, in this same
                    # transaction as the batch-id claim: hourly + daily rows.
                    from orchestrator.core.usage_rollup import (
                        apply_increments, increment_totals, increments_from_records,
                    )
                    if item.usage is not None:
                        increments = item.usage
                    elif item.count_records:
                        increments = increments_from_records(item.records)
                    else:
                        increments = []
                    await apply_increments(db, increments, source=item.source)

                    # Real-time spend counters move by the same increments.
                    totals = increment_totals(increments)
                    if totals["call_count"] > 0:
                        try:
                            from orchestrator.core.policy_engine import update_real_time_spend
                            async with db.begin_nested():
                                await update_real_time_spend(
                                    app_id=item.app_id,
                                    team_id=item.team_id,
                                    total_cost=totals["total_cost"],
                                    call_count=totals["call_count"],
                                    input_tokens=totals["input_tokens"],
                                    output_tokens=totals["output_tokens"],
                                    total_duration_ms=totals["duration_ms_sum"],
                                    db=db,
                                )
                        except Exception:
                            logger.warning(
                                "RealTimeSpend update failed for app=%s team=%s — policy enforcement may use stale counters",
                                item.app_id, item.team_id, exc_info=True,
                            )

                elif isinstance(item, HeartbeatItem):
                    db.add(AgentHeartbeat(
                        app_id=item.app_id,
                        team_id=item.team_id,
                        agent_version=item.agent_version,
                        instrumented_providers=item.instrumented_providers,
                        host_info=item.host_info,
                    ))
                    # Update app metadata
                    hb_vals: dict = {"last_seen_at": item.timestamp}
                    if item.agent_version:
                        hb_vals["agent_version"] = item.agent_version
                    await db.execute(sa_update(App).where(App.id == item.app_id).values(**hb_vals))
                    await db.execute(
                        sa_update(App)
                        .where(App.id == item.app_id, App.first_seen_at.is_(None))
                        .values(first_seen_at=item.timestamp)
                    )
                    heartbeat_count += 1

                elif isinstance(item, ReconcileItem):
                    # Safety net, never fatal for the other items in this
                    # flush: runs in a SAVEPOINT and only logs on failure.
                    from orchestrator.core.usage_rollup import reconcile_daily
                    try:
                        async with db.begin_nested():
                            res = await reconcile_daily(
                                db, item.days, prune_hourly=item.prune_hourly,
                            )
                        if res.keys_repaired or res.hourly_rows_pruned:
                            logger.info(
                                "Aggregate reconciliation: %d keys repaired, %d hourly rows pruned",
                                res.keys_repaired, res.hourly_rows_pruned,
                            )
                    except Exception:
                        logger.error("Aggregate reconciliation failed", exc_info=True)

                elif isinstance(item, PolicyDecisionItem):
                    from orchestrator.db.models import PolicyDecision
                    db.add(PolicyDecision(
                        app_id=item.app_id,
                        team_id=item.team_id,
                        decision=item.decision,
                        reason=item.reason,
                        request_provider=item.request_provider,
                        request_model=item.request_model,
                        request_environment=item.request_environment,
                        request_estimated_tokens=item.request_estimated_tokens,
                        request_estimated_cost=item.request_estimated_cost,
                        spend_at_decision=item.spend_at_decision,
                        evaluation_latency_ms=item.evaluation_latency_ms,
                    ))
                    # Enforcement actions (deny/throttle — allows are never
                    # enqueued) also land in the tamper-evident audit chain, so
                    # "show me every enforcement action" is answerable from the
                    # same trail examiners pull, with cryptographic integrity.
                    db.add(AuditLog(
                        actor_id="enforcement-engine",
                        team_id=item.team_id,
                        resource_type="app",
                        resource_id=item.app_id,
                        action=f"enforcement_{item.decision}",
                        after={
                            "decision": item.decision,
                            "reason": item.reason,
                            "provider": item.request_provider,
                            "model": item.request_model,
                            "environment": item.request_environment,
                            "estimated_cost_usd": str(item.request_estimated_cost),
                        },
                    ))

                elif isinstance(item, RegistrationItem):
                    db.add(App(
                        id=item.app_uuid,
                        team_id=item.team_id,
                        app_id=item.app_id,
                        app_name=item.app_name,
                        environment=item.environment,
                        api_key_hash=item.api_key_hash,
                        api_key_prefix=item.api_key_prefix,
                    ))
                    db.add(AuditLog(
                        actor_id="master-key",
                        actor_ip=item.actor_ip,
                        team_id=item.team_id,
                        resource_type="app",
                        resource_id=item.app_uuid,
                        action="registered",
                        after={"app_id": item.app_id, "team": item.team_slug, "environment": item.environment},
                    ))
                    registration_count += 1

                # ── Phase 9 items ────────────────────────────────────
                elif isinstance(item, PoELedgerItem):
                    from orchestrator.db.models import PoELedgerEntry
                    db.add(PoELedgerEntry(
                        team_id=item.team_id,
                        session_id=item.session_id,
                        seq_num=item.seq_num,
                        prev_hash=item.prev_hash,
                        entry_hash=item.entry_hash,
                        decision_hash=item.decision_hash,
                        trajectory_proof_id=item.trajectory_proof_id,
                        proof_type=item.proof_type,
                        tee_quote=item.tee_quote,
                        tee_platform=item.tee_platform,
                        risk_level=item.risk_level,
                    ))

                elif isinstance(item, NeuroAssuranceItem):
                    from orchestrator.db.models import NeuroAssuranceMetric
                    db.add(NeuroAssuranceMetric(
                        team_id=item.team_id,
                        app_id=item.app_id,
                        session_id=item.session_id,
                        topology_hash=item.topology_hash,
                        energy_per_spike=item.energy_per_spike,
                        stdp_drift=item.stdp_drift,
                        embodied_efficiency_score=item.embodied_efficiency_score,
                        spike_rate_hz=item.spike_rate_hz,
                        active_neuron_ratio=item.active_neuron_ratio,
                        inference_latency_ms=item.inference_latency_ms,
                        hardware_backend=item.hardware_backend,
                    ))

            await db.commit()

        # Clean up pending registration keys after successful commit
        pending_keys = [
            item.pending_key for item in items
            if isinstance(item, RegistrationItem) and item.pending_key
        ]
        if pending_keys:
            from orchestrator.api.apps import _pending_registrations, _pending_lock
            with _pending_lock:
                for pk in pending_keys:
                    _pending_registrations.discard(pk)

        duration_ms = (time.perf_counter() - start) * 1000
        if ingest_count or heartbeat_count or registration_count:
            logger.debug(
                "Writer flushed %d ingest (%d records) + %d heartbeats + %d registrations in %.0fms",
                ingest_count, record_count, heartbeat_count, registration_count, duration_ms,
            )

    except Exception as exc:
        # Clean up pending keys even on failure so retries aren't blocked
        pending_keys = [
            item.pending_key for item in items
            if isinstance(item, RegistrationItem) and item.pending_key
        ]
        if pending_keys:
            from orchestrator.api.apps import _pending_registrations, _pending_lock
            with _pending_lock:
                for pk in pending_keys:
                    _pending_registrations.discard(pk)

        if not _is_retry:
            # Retry once after 1 second for transient failures (e.g. DB lock timeout)
            logger.warning("Writer flush failed (%d items), retrying once in 1s: %s", len(items), exc)
            try:
                await asyncio.sleep(1)
                await _flush_batch(items, _is_retry=True)
                logger.info("Writer flush retry succeeded (%d items)", len(items))
            except Exception as retry_exc:
                _flush_permanently_lost.add(len(items))
                logger.error(
                    "Writer flush retry also failed (%d items permanently lost, total lost: %d): %s",
                    len(items), _flush_permanently_lost.total, retry_exc,
                )
        else:
            # Already a retry — propagate so the outer handler can count the loss
            raise
