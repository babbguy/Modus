"""
Tests for orchestrator.core.write_queue
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import patch, AsyncMock

import pytest

from orchestrator.core.write_queue import (
    IngestItem,
    HeartbeatItem,
    PolicyDecisionItem,
    BATCH_SIZE,
    QUEUE_MAX,
    BACKPRESSURE_THRESHOLD,
    queue_pressure,
    queue_over_pressure,
    get_queue,
    enqueue,
    start_writer,
    stop_writer,
    _drain_queue,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def _reset_queue():
    """Reset the write queue module state between tests."""
    import orchestrator.core.write_queue as wq
    old_queue = wq._queue
    old_task = wq._writer_task
    yield
    # Stop any running writer
    if wq._writer_task and not wq._writer_task.done():
        wq._writer_task.cancel()
        try:
            await wq._writer_task
        except (asyncio.CancelledError, Exception):
            pass
    wq._queue = old_queue
    wq._writer_task = old_task


# ── Queue state tests ────────────────────────────────────────────────────────


class TestQueueState:
    def test_queue_pressure_not_started(self):
        import orchestrator.core.write_queue as wq
        saved = wq._queue
        wq._queue = None
        try:
            assert queue_pressure() == 0.0
        finally:
            wq._queue = saved

    def test_queue_over_pressure_not_started(self):
        import orchestrator.core.write_queue as wq
        saved = wq._queue
        wq._queue = None
        try:
            assert queue_over_pressure() is False
        finally:
            wq._queue = saved

    def test_get_queue_raises_when_not_started(self):
        import orchestrator.core.write_queue as wq
        saved = wq._queue
        wq._queue = None
        try:
            with pytest.raises(RuntimeError, match="not started"):
                get_queue()
        finally:
            wq._queue = saved


# ── Enqueue tests ─────────────────────────────────────────────────────────


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_ingest_item(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        item = IngestItem(
            app_id="app-1",
            team_id="team-1",
            batch_id="batch-1",
            records=[{"model": "gpt-4"}],
            record_count=1,
            total_cost=Decimal("0.01"),
        )
        result = await enqueue(item)
        assert result is True
        assert wq._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_heartbeat_item(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        item = HeartbeatItem(app_id="app-1", team_id="team-1")
        result = await enqueue(item)
        assert result is True

    @pytest.mark.asyncio
    async def test_enqueue_drops_when_full(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        # Fill the queue to max
        for i in range(QUEUE_MAX):
            wq._queue.put_nowait(f"item-{i}")
        item = HeartbeatItem(app_id="app-1", team_id="team-1")
        result = await enqueue(item)
        assert result is False


# ── Backpressure tests ───────────────────────────────────────────────────────


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_pressure_ratio(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        for i in range(100):
            wq._queue.put_nowait(f"item-{i}")
        pressure = queue_pressure()
        assert pressure == pytest.approx(100 / QUEUE_MAX, rel=0.01)

    @pytest.mark.asyncio
    async def test_over_pressure_at_threshold(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        for i in range(BACKPRESSURE_THRESHOLD):
            wq._queue.put_nowait(f"item-{i}")
        assert queue_over_pressure() is True


# ── Drain tests ──────────────────────────────────────────────────────────────


class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_empty_queue(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        items = await _drain_queue()
        assert items == []

    @pytest.mark.asyncio
    async def test_drain_partial(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        for i in range(5):
            wq._queue.put_nowait(f"item-{i}")
        items = await _drain_queue()
        assert len(items) == 5

    @pytest.mark.asyncio
    async def test_drain_respects_batch_size(self):
        import orchestrator.core.write_queue as wq
        wq._queue = asyncio.Queue()
        for i in range(BATCH_SIZE + 50):
            wq._queue.put_nowait(f"item-{i}")
        items = await _drain_queue()
        assert len(items) == BATCH_SIZE
        assert wq._queue.qsize() == 50


# ── Start/Stop lifecycle tests ───────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_queue_and_task(self):
        import orchestrator.core.write_queue as wq
        wq._queue = None
        wq._writer_task = None

        # Patch _writer_loop to not actually run DB operations
        async def mock_loop():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

        with patch.object(wq, "_writer_loop", mock_loop):
            await start_writer()

        assert wq._queue is not None
        assert wq._writer_task is not None
        assert not wq._writer_task.done()

        # Clean up
        wq._writer_task.cancel()
        try:
            await wq._writer_task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        import orchestrator.core.write_queue as wq

        async def mock_loop():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

        with patch.object(wq, "_writer_loop", mock_loop):
            await start_writer()

        assert wq._writer_task is not None

        with patch.object(wq, "_flush_batch", new_callable=AsyncMock):
            await stop_writer()

        assert wq._writer_task is None


# ── PolicyDecisionItem tests ─────────────────────────────────────────────────


class TestPolicyDecisionItem:
    def test_creation(self):
        item = PolicyDecisionItem(
            app_id="app-1",
            team_id="team-1",
            decision="deny",
            reason="over budget",
            request_provider="openai",
            request_model="gpt-4",
            request_environment="production",
            request_estimated_tokens=1000,
            request_estimated_cost=Decimal("0.03"),
            spend_at_decision=Decimal("50.00"),
            evaluation_latency_ms=5,
        )
        assert item.decision == "deny"
        assert item.request_estimated_cost == Decimal("0.03")


async def test_aggregate_upsert_folds_min_max_and_ignores_null(engine, registered_app):
    """Repeated aggregate upserts keep the true min/max and never let a NULL
    stored value erase a real one (SQLite min(NULL, x) is NULL)."""
    import uuid
    from datetime import datetime, timezone
    from decimal import Decimal

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from orchestrator.core import write_queue as wq
    from orchestrator.db import session as session_mod
    from orchestrator.db.models import UsageAggregate

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    prev = session_mod._session_factory
    session_mod._session_factory = factory
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    def row(min_ms, max_ms, calls, dur_sum):
        return {
            "id": str(uuid.uuid4()), "app_id": registered_app["app_uuid"],
            "team_id": registered_app["team_id"], "provider": "anthropic",
            "model": "claude-sonnet-5-5", "resource_type": "llm_call",
            "granularity": "hourly", "period_start": start,
            "period_end": start.replace(hour=13), "call_count": calls,
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "input_cost": Decimal("0.001"), "output_cost": Decimal("0.001"),
            "total_cost": Decimal("0.002"), "avg_duration_ms": None,
            "min_duration_ms": min_ms, "max_duration_ms": max_ms,
            "duration_ms_sum": dur_sum, "source": "sdk",
        }

    try:
        await wq._flush_batch([wq.AggregationItem(rows=[row(None, None, 1, 0)], granularity="hourly")])
        await wq._flush_batch([wq.AggregationItem(rows=[row(300, 300, 1, 300)], granularity="hourly")])
        await wq._flush_batch([wq.AggregationItem(rows=[row(100, 500, 2, 600)], granularity="hourly")])
        async with factory() as db:
            agg = (await db.execute(select(UsageAggregate))).scalar_one()
    finally:
        session_mod._session_factory = prev

    assert agg.call_count == 4
    assert agg.min_duration_ms == 100
    assert agg.max_duration_ms == 500
    assert agg.duration_ms_sum == 900
    assert agg.total_cost == Decimal("0.006")


async def test_duplicate_ingest_batch_is_skipped_without_losing_other_items(engine, registered_app):
    """A re-sent batch_id is ignored, and does not roll back other items that
    share the same writer flush."""
    import uuid
    from datetime import datetime, timezone
    from decimal import Decimal

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from orchestrator.core import write_queue as wq
    from orchestrator.db import session as session_mod
    from orchestrator.db.models import IngestBatch, UsageRecord

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    prev = session_mod._session_factory
    session_mod._session_factory = factory

    def item(batch_id):
        rec = {
            "id": str(uuid.uuid4()), "app_id": registered_app["app_uuid"],
            "team_id": registered_app["team_id"], "provider": "openai",
            "resource_type": "llm_call", "model": "gpt-5", "input_tokens": 10,
            "output_tokens": 5, "total_tokens": 15, "input_cost": Decimal("0.01"),
            "output_cost": Decimal("0.01"), "total_cost": Decimal("0.02"),
            "timestamp": datetime(2026, 10, 1, tzinfo=timezone.utc),
        }
        return wq.IngestItem(
            app_id=registered_app["app_uuid"], team_id=registered_app["team_id"],
            batch_id=batch_id, records=[rec], record_count=1,
        )

    try:
        await wq._flush_batch([item("batch-original-0001")])
        # The duplicate shares a flush with a new, legitimate batch.
        await wq._flush_batch([item("batch-original-0001"), item("batch-new-000002")])
        async with factory() as db:
            records = (await db.execute(select(func.count()).select_from(UsageRecord))).scalar_one()
            batches = (await db.execute(select(func.count()).select_from(IngestBatch))).scalar_one()
    finally:
        session_mod._session_factory = prev

    assert batches == 2
    assert records == 2
