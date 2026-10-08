"""
Tests — Database Session Management
Covers: sqlite_dt, build_engine, get_engine, get_session, get_session_ctx,
        get_read_session, close_db.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
def test_sqlite_dt_format():
    from orchestrator.db.session import sqlite_dt
    dt = datetime(2026, 3, 18, 14, 30, 0, tzinfo=timezone.utc)
    result = sqlite_dt(dt)
    assert result == "2026-03-18 14:30:00.000000"


def test_sqlite_dt_midnight():
    from orchestrator.db.session import sqlite_dt
    dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = sqlite_dt(dt)
    assert result == "2026-01-01 00:00:00.000000"


def test_sqlite_dt_converts_to_utc_and_matches_sqlalchemy_storage_format():
    from orchestrator.db.session import sqlite_dt
    from datetime import timedelta
    plus2 = timezone(timedelta(hours=2))
    assert sqlite_dt(datetime(2026, 6, 15, 14, 45, 30, 999999, tzinfo=plus2)) == "2026-06-15 12:45:30.000000"
    assert sqlite_dt(datetime(2026, 6, 15, 12, 45, 30)) == "2026-06-15 12:45:30.000000"


async def test_raw_sql_range_includes_a_row_exactly_on_the_boundary(db_session):
    """Regression: with an ISO 'T' bound parameter a daily aggregate stored at
    00:00:00 on the 1st fell outside `period_start >= :start`, so month-to-date
    totals on SQLite silently dropped the first day of the month."""
    from decimal import Decimal
    from sqlalchemy import text
    from orchestrator.db.models import App, Team, UsageAggregate
    from orchestrator.db.session import sqlite_dt

    team = Team(slug="b-team", name="B")
    db_session.add(team)
    await db_session.flush()
    app = App(team_id=team.id, app_id="b-app", app_name="B", api_key_hash="h", api_key_prefix="p")
    db_session.add(app)
    await db_session.flush()
    first = datetime(2026, 9, 1, tzinfo=timezone.utc)
    db_session.add(UsageAggregate(
        app_id=app.id, team_id=team.id, provider="openai", model="m", resource_type="llm_call",
        granularity="daily", period_start=first, period_end=first, total_cost=Decimal("5"),
    ))
    await db_session.commit()
    n = (await db_session.execute(
        text("SELECT COUNT(*) FROM usage_aggregates WHERE period_start >= :s AND period_start < :e"),
        {"s": sqlite_dt(first), "e": sqlite_dt(datetime(2026, 9, 2, tzinfo=timezone.utc))},
    )).scalar_one()
    assert n == 1


def test_get_engine_not_initialized():
    from orchestrator.db import session as session_mod
    saved = session_mod._engine
    try:
        session_mod._engine = None
        with pytest.raises(RuntimeError, match="not initialised"):
            session_mod.get_engine()
    finally:
        session_mod._engine = saved


def test_get_session_not_initialized():
    from orchestrator.db import session as session_mod
    saved = session_mod._session_factory
    try:
        session_mod._session_factory = None
        # get_session is an async generator, but checking the factory guard
        with pytest.raises(RuntimeError, match="not initialised"):
            gen = session_mod.get_session()
            # Need to advance the generator to hit the guard
            import asyncio
            asyncio.run(gen.__anext__())
    except RuntimeError:
        pass
    finally:
        session_mod._session_factory = saved


async def test_get_session_ctx_not_initialized():
    from orchestrator.db import session as session_mod
    saved = session_mod._session_factory
    try:
        session_mod._session_factory = None
        with pytest.raises(RuntimeError, match="not initialised"):
            async with session_mod.get_session_ctx() as _:
                pass
    finally:
        session_mod._session_factory = saved


async def test_build_engine_sqlite():
    from orchestrator.db.session import build_engine
    engine = build_engine()
    assert engine is not None
    await engine.dispose()


async def test_get_read_session_not_initialized():
    from orchestrator.db import session as session_mod
    saved_factory = session_mod._session_factory
    saved_replica = session_mod._replica_session_factory
    try:
        session_mod._session_factory = None
        session_mod._replica_session_factory = None
        with pytest.raises(RuntimeError, match="not initialised"):
            gen = session_mod.get_read_session()
            await gen.__anext__()
    except RuntimeError:
        pass
    finally:
        session_mod._session_factory = saved_factory
        session_mod._replica_session_factory = saved_replica


async def test_close_db_no_engine():
    """close_db should not crash when engine is None."""
    from orchestrator.db import session as session_mod
    saved = session_mod._engine
    saved_replica = session_mod._replica_engine
    try:
        session_mod._engine = None
        session_mod._replica_engine = None
        await session_mod.close_db()
    finally:
        session_mod._engine = saved
        session_mod._replica_engine = saved_replica


def test_sqlite_dt_returns_aware_datetime_on_postgres(monkeypatch):
    """asyncpg rejects str for timestamptz parameters (finance/insights queries
    returned HTTP 500 on PostgreSQL), so non-SQLite must get a real datetime."""
    from datetime import datetime, timezone
    from orchestrator.core.config import get_settings
    from orchestrator.db.session import sqlite_dt

    cfg = get_settings()
    monkeypatch.setattr(type(cfg), "is_sqlite", property(lambda self: False))
    out = sqlite_dt(datetime(2026, 3, 18, 1, 2, 3, 456789))
    assert out == datetime(2026, 3, 18, 1, 2, 3, tzinfo=timezone.utc)
    assert out.tzinfo is not None
