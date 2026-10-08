"""
Maintenance tasks coverage.

Targets orchestrator.core.maintenance: partition creation, staleness
detection, budget reset, prune functions, notify_app_pause.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.db.models import Base
@pytest_asyncio.fixture
async def maint_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def maint_factory(maint_engine):
    return async_sessionmaker(maint_engine, class_=AsyncSession, expire_on_commit=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers — create team + app so FK constraints are satisfied
# ═══════════════════════════════════════════════════════════════════════════════

async def _seed_team_and_app(factory):
    """Create a team and app, return (team_id, app_id)."""
    import hashlib
    from orchestrator.db.models import Team, App
    team_id = str(uuid.uuid4())
    key_raw = f"mds_{uuid.uuid4().hex[:16]}"
    key_hash = hashlib.sha256(key_raw.encode()).hexdigest()
    async with factory() as session:
        team = Team(id=team_id, slug=f"t-{team_id[:8]}", name="Test Team")
        session.add(team)
        await session.flush()
        app = App(
            app_id=key_raw,
            app_name="Test App",
            team_id=team_id,
            is_active=True,
            api_key_hash=key_hash,
            api_key_prefix=key_raw[:8],
        )
        session.add(app)
        await session.flush()
        app_uuid = str(app.id)
        await session.commit()
    return team_id, app_uuid


# ═══════════════════════════════════════════════════════════════════════════════
# 1. BATCHED DELETE (no session factory → returns 0)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_batched_delete_no_factory():
    from orchestrator.core import maintenance as mod
    original = mod._session_factory
    try:
        mod._session_factory = None
        from orchestrator.db.models import AgentHeartbeat
        result = await mod._batched_delete(
            AgentHeartbeat, AgentHeartbeat.received_at,
            datetime.now(timezone.utc), AgentHeartbeat.id,
            label="test",
        )
        assert result == 0
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PRUNE HEARTBEATS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_prune_heartbeats(maint_factory):
    from orchestrator.core import maintenance as mod
    from orchestrator.db.models import AgentHeartbeat
    original = mod._session_factory
    try:
        mod._session_factory = maint_factory
        team_id, app_uuid = await _seed_team_and_app(maint_factory)
        async with maint_factory() as session:
            hb = AgentHeartbeat(
                app_id=app_uuid,
                team_id=team_id,
                agent_version="1.0",
                received_at=datetime.now(timezone.utc) - timedelta(days=365),
            )
            session.add(hb)
            await session.commit()

        await mod.prune_heartbeats()
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ENSURE PARTITIONS (SQLite → no-op)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_ensure_partitions_sqlite():
    from orchestrator.core.maintenance import ensure_partitions
    await ensure_partitions()


async def test_ensure_partitions_no_factory():
    from orchestrator.core import maintenance as mod
    original = mod._session_factory
    try:
        mod._session_factory = None
        await mod.ensure_partitions()
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CHECK INGEST STALENESS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_check_ingest_staleness_no_factory():
    from orchestrator.core import maintenance as mod
    original = mod._session_factory
    try:
        mod._session_factory = None
        await mod.check_ingest_staleness()
    finally:
        mod._session_factory = original


async def test_check_ingest_staleness_empty(maint_factory):
    """With no usage records, should log a staleness warning."""
    from orchestrator.core import maintenance as mod
    original = mod._session_factory
    try:
        mod._session_factory = maint_factory
        await mod.check_ingest_staleness()
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 5. AUTO RESET BUDGET SUSPENSIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_auto_reset_no_factory():
    from orchestrator.core import maintenance as mod
    original = mod._session_factory
    try:
        mod._session_factory = None
        await mod.auto_reset_budget_suspensions()
    finally:
        mod._session_factory = original


async def test_auto_reset_with_suspended_app(maint_factory):
    """Create a budget_suspended app from yesterday — should be reset."""
    import hashlib
    from orchestrator.core import maintenance as mod
    from orchestrator.db.models import App, Team
    from sqlalchemy import select
    original = mod._session_factory
    try:
        mod._session_factory = maint_factory
        team_id = str(uuid.uuid4())
        key_raw = "mds_test123abc"
        key_hash = hashlib.sha256(key_raw.encode()).hexdigest()
        async with maint_factory() as session:
            team = Team(id=team_id, slug="maint-team", name="Maint")
            session.add(team)
            await session.flush()
            app = App(
                app_id=key_raw,
                app_name="Suspended App",
                team_id=team_id,
                is_active=True,
                api_key_hash=key_hash,
                api_key_prefix=key_raw[:8],
                enforcement_state="budget_suspended",
                enforcement_suspended_at=datetime.now(timezone.utc) - timedelta(days=1),
                enforcement_suspended_reason="Daily budget exceeded",
            )
            session.add(app)
            await session.commit()

        await mod.auto_reset_budget_suspensions()

        async with maint_factory() as session:
            result = await session.execute(
                select(App).where(App.app_name == "Suspended App")
            )
            app = result.scalar_one()
            assert app.enforcement_state == "active"
            assert app.enforcement_suspended_at is None
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 6. PRUNE REAL-TIME SPEND
# ═══════════════════════════════════════════════════════════════════════════════

async def test_prune_real_time_spend(maint_factory):
    from orchestrator.core import maintenance as mod
    from orchestrator.db.models import RealTimeSpend
    original = mod._session_factory
    try:
        mod._session_factory = maint_factory
        team_id, app_uuid = await _seed_team_and_app(maint_factory)
        async with maint_factory() as session:
            rts = RealTimeSpend(
                app_id=app_uuid,
                team_id=team_id,
                period="daily",
                window_key="2025-01-01",
                window_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
                window_end=datetime(2025, 1, 2, tzinfo=timezone.utc),
                total_cost=0,
                call_count=0,
                input_tokens=0,
                output_tokens=0,
            )
            session.add(rts)
            await session.commit()

        await mod.prune_real_time_spend()
    finally:
        mod._session_factory = original


# ═══════════════════════════════════════════════════════════════════════════════
# 7. NOTIFY APP PAUSE
# ═══════════════════════════════════════════════════════════════════════════════

async def test_notify_app_pause_success():
    from orchestrator.core.maintenance import notify_app_pause
    with patch("orchestrator.core.maintenance.urllib_request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_resp

        result = await notify_app_pause(
            app_id="test-id",
            app_name="Test App",
            pause_url="http://localhost:8000/pause",
            reason="Budget exceeded",
        )
        assert result is True


async def test_notify_app_pause_failure():
    from orchestrator.core.maintenance import notify_app_pause
    with patch("orchestrator.core.maintenance.urllib_request.urlopen") as mock_open:
        mock_open.side_effect = Exception("Connection refused")
        result = await notify_app_pause(
            app_id="test-id",
            app_name="Test App",
            pause_url="http://localhost:8000/pause",
            reason="Budget exceeded",
        )
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. BATCH_DELETE_SIZE CONSTANT
# ═══════════════════════════════════════════════════════════════════════════════

def test_batch_delete_size():
    from orchestrator.core.maintenance import BATCH_DELETE_SIZE
    assert BATCH_DELETE_SIZE == 5000
