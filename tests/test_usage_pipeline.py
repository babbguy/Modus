"""
Regression tests — usage-data pipeline
=======================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Covers the bugs fixed on fix/usage-pipeline:
  1. the aggregator re-added the last 2 days of raw usage every cycle
  2. dashboard KPIs ignored SDK (hourly) usage until a (buggy) rollup
  3. /ingest dropped the session id, so SDK sessions never reached
     the Sessions view / attribution engine
  4. "vs prior 7d" compared against a 14-day total
  5. anomaly detection (and forecasting) never ran on SQLite
  6. the API rate limit throttled normal SDK traffic and the SDK treated
     429 as "unreachable" (fail-open, stop enforcing)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import (
    AnomalyEvent, App, AttributionSession, RealTimeSpend, SpendForecast,
    Team, UsageAggregate, UsageRecord,
)

NOW = datetime.now(timezone.utc)
HOUR = NOW.replace(minute=0, second=0, microsecond=0)


# ── Fixtures / helpers ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def wq(engine):
    """A write queue whose items the test flushes explicitly (no writer task)."""
    from orchestrator.core import write_queue as wq_mod
    from orchestrator.db import session as session_mod

    prev_q, prev_f = wq_mod._queue, session_mod._session_factory
    wq_mod._queue = asyncio.Queue()
    session_mod._session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    yield wq_mod
    wq_mod._queue = prev_q
    session_mod._session_factory = prev_f


async def _drain(wq_mod) -> None:
    items = []
    while not wq_mod._queue.empty():
        items.append(wq_mod._queue.get_nowait())
    if items:
        await wq_mod._flush_batch(items)


def _headers(app):
    return {"X-Modus-APIKey": app["api_key"]}


def _agg(provider, model, calls, cost, start=HOUR, **kw):
    return {
        "provider": provider, "model": model, "resource_type": "llm_call",
        "call_count": calls, "input_tokens": 100 * calls, "output_tokens": 10 * calls,
        "total_tokens": 110 * calls, "input_cost": "0", "output_cost": str(cost),
        "total_cost": str(cost), "duration_ms_sum": 50 * calls,
        "duration_ms_min": 40, "duration_ms_max": 60,
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(minutes=5)).isoformat(),
        **kw,
    }


def _rec(provider, model, cost, ts=None, metadata=None):
    return {
        "provider": provider, "model": model, "resource_type": "llm_call",
        "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
        "input_cost": "0", "output_cost": str(cost), "total_cost": str(cost),
        "duration_ms": 50, "timestamp": (ts or NOW).isoformat(),
        "metadata": metadata,
    }


async def _summary(client):
    r = await client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200, r.text
    return r.json()


async def _agg_totals(engine, granularity):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = (await db.execute(
            select(func.coalesce(func.sum(UsageAggregate.call_count), 0),
                   func.coalesce(func.sum(UsageAggregate.total_cost), 0))
            .where(UsageAggregate.granularity == granularity)
        )).one()
    return int(row[0]), Decimal(str(row[1])).quantize(Decimal("0.00000001"))


# ── Bug 1 + 2: counted once, current, idempotent ─────────────────────────────

async def test_sdk_aggregated_usage_is_current_in_overview_and_finance(client, registered_app, wq):
    """SDK hourly uploads appear in today/7d/MTD immediately (no rollup wait),
    and overview MTD equals finance MTD."""
    payload = {
        "batch_id": str(uuid.uuid4()), "format": "aggregated",
        "traces_counted_in_aggregates": True,
        "aggregates": [_agg("openai", "gpt-4o", 10, "1.25"), _agg("anthropic", "claude-x", 5, "0.75")],
        "traces": [_rec("openai", "gpt-4o", "0.125")],
    }
    r = await client.post("/api/v1/ingest", json=payload, headers=_headers(registered_app))
    assert r.status_code == 202 and r.json()["accepted"] == 15
    await _drain(wq)

    s = await _summary(client)
    for k in ("total_cost_today", "total_cost_7d", "total_cost_30d", "total_cost_mtd"):
        assert Decimal(str(s[k])) == Decimal("2.00"), (k, s[k])
    assert s["total_calls_today"] == 15

    fin = (await client.get("/api/v1/finance/summary")).json()
    assert Decimal(fin["total_current_spend_usd"]) == Decimal(str(s["total_cost_mtd"])).quantize(Decimal("0.01"))


async def test_aggregation_cycle_repeated_never_changes_totals(client, registered_app, wq, engine):
    """Raw + SDK usage across today and yesterday; running the aggregation
    cycle many times leaves every total unchanged and daily == sum(hourly)."""
    from orchestrator.core.aggregator import run_aggregation_cycle

    yesterday = NOW - timedelta(days=1)
    raw = {"batch_id": str(uuid.uuid4()), "records": [
        _rec("openai", "gpt-4o", "0.10", ts=yesterday),
        _rec("openai", "gpt-4o", "0.20", ts=NOW),
        _rec("openai", None, "0.05", ts=NOW),  # NULL model path
    ]}
    agg = {"batch_id": str(uuid.uuid4()), "format": "aggregated",
           "traces_counted_in_aggregates": True,
           "aggregates": [_agg("openai", "gpt-4o", 4, "0.40")], "traces": []}
    for p in (raw, agg):
        r = await client.post("/api/v1/ingest", json=p, headers=_headers(registered_app))
        assert r.status_code == 202
    await _drain(wq)

    expected = (7, Decimal("0.75000000"))
    assert await _agg_totals(engine, "hourly") == expected
    assert await _agg_totals(engine, "daily") == expected
    before = await _summary(client)

    for _ in range(5):
        await run_aggregation_cycle()
        await _drain(wq)

    assert await _agg_totals(engine, "hourly") == expected
    assert await _agg_totals(engine, "daily") == expected
    assert await _summary(client) == before


async def test_retried_batch_is_counted_once(client, registered_app, wq, engine):
    """The same aggregated batch id sent twice (SDK retry after a lost
    response) adds its usage once — aggregates, records and real-time spend."""
    payload = {
        "batch_id": str(uuid.uuid4()), "format": "aggregated",
        "traces_counted_in_aggregates": True,
        "aggregates": [_agg("openai", "gpt-4o", 3, "0.30")],
        "traces": [_rec("openai", "gpt-4o", "0.10")],
    }
    for _ in range(2):
        r = await client.post("/api/v1/ingest", json=payload, headers=_headers(registered_app))
        assert r.status_code == 202
        await _drain(wq)

    assert await _agg_totals(engine, "hourly") == (3, Decimal("0.30000000"))
    assert await _agg_totals(engine, "daily") == (3, Decimal("0.30000000"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        assert (await db.execute(select(func.count()).select_from(UsageRecord))).scalar_one() == 1
        rts = (await db.execute(
            select(RealTimeSpend).where(RealTimeSpend.period == "daily")
        )).scalar_one()
        assert Decimal(str(rts.total_cost)) == Decimal("0.30")
        assert rts.call_count == 3


async def test_traces_are_detail_not_counted_again(client, registered_app, wq, engine):
    """Current SDKs fold every traced call into the aggregates; legacy SDKs
    left violations/errors out, so only those legacy traces are counted."""
    new_sdk = {
        "batch_id": str(uuid.uuid4()), "format": "aggregated",
        "traces_counted_in_aggregates": True,
        "aggregates": [_agg("openai", "gpt-4o", 2, "0.20")],
        "traces": [_rec("openai", "gpt-4o", "0.10", metadata={"_policy_violation": True}),
                   _rec("openai", "gpt-4o", "0.10")],
    }
    legacy = {
        "batch_id": str(uuid.uuid4()), "format": "aggregated",
        "aggregates": [_agg("openai", "gpt-4o", 1, "0.10")],
        "traces": [_rec("openai", "gpt-4o", "0.05", metadata={"_error": True}),
                   _rec("openai", "gpt-4o", "0.10")],  # sampled: already in aggregate
    }
    for p in (new_sdk, legacy):
        r = await client.post("/api/v1/ingest", json=p, headers=_headers(registered_app))
        assert r.status_code == 202
    await _drain(wq)
    assert await _agg_totals(engine, "daily") == (4, Decimal("0.35000000"))


async def test_reconcile_repairs_drift_by_replacing(wq, engine, registered_app, client):
    """A daily row that disagrees with its hourly rows is REPLACED by the
    hourly sum (never added to)."""
    from orchestrator.core.aggregator import run_aggregation_cycle

    r = await client.post("/api/v1/ingest", headers=_headers(registered_app), json={
        "batch_id": str(uuid.uuid4()), "records": [_rec("openai", "gpt-4o", "0.50")]})
    assert r.status_code == 202
    await _drain(wq)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        daily = (await db.execute(
            select(UsageAggregate).where(UsageAggregate.granularity == "daily"))).scalar_one()
        daily.total_cost = Decimal("9.99")
        daily.call_count = 42
        await db.commit()
    for _ in range(3):
        await run_aggregation_cycle()
        await _drain(wq)
    assert await _agg_totals(engine, "daily") == (1, Decimal("0.50000000"))


async def test_retention_prunes_hourly_without_losing_usage(engine, registered_app):
    """Hourly rows past retention are deleted; daily totals are untouched, and
    a key that only had hourly rows (legacy data) is filled into daily first."""
    from orchestrator.core import maintenance as mod
    from orchestrator.core.usage_rollup import apply_increments

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    old = (NOW - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    inc = {
        "app_id": registered_app["app_uuid"], "team_id": registered_app["team_id"],
        "provider": "openai", "model": "gpt-4o", "resource_type": "llm_call",
        "hour_start": old, "call_count": 2, "input_tokens": 1, "output_tokens": 1,
        "total_tokens": 2, "input_cost": Decimal("0"), "output_cost": Decimal("0.2"),
        "total_cost": Decimal("0.2"), "duration_ms_sum": 0,
        "min_duration_ms": None, "max_duration_ms": None,
    }
    async with factory() as db:
        await apply_increments(db, [inc], source="ingest")
        # Legacy: an hourly-only key with no daily row
        db.add(UsageAggregate(
            app_id=registered_app["app_uuid"], team_id=registered_app["team_id"],
            provider="anthropic", model="legacy", resource_type="llm_call",
            granularity="hourly", period_start=old, period_end=old + timedelta(hours=1),
            call_count=1, total_cost=Decimal("0.7"),
        ))
        await db.commit()

    prev = mod._session_factory
    mod._session_factory = factory
    try:
        await mod.compact_hourly_to_daily()
        await mod.compact_hourly_to_daily()
    finally:
        mod._session_factory = prev

    assert await _agg_totals(engine, "hourly") == (0, Decimal("0E-8"))
    assert await _agg_totals(engine, "daily") == (3, Decimal("0.90000000"))


async def test_raw_record_retention_does_not_change_aggregates(client, registered_app, wq, engine):
    from orchestrator.core import maintenance as mod

    r = await client.post("/api/v1/ingest", headers=_headers(registered_app), json={
        "batch_id": str(uuid.uuid4()),
        "records": [_rec("openai", "gpt-4o", "0.30", ts=NOW - timedelta(hours=30))]})
    assert r.status_code == 202
    await _drain(wq)
    before = await _agg_totals(engine, "daily")
    prev = mod._session_factory
    mod._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await mod.compact_usage_records()
        await mod.compact_usage_records()
    finally:
        mod._session_factory = prev
    assert before == (1, Decimal("0.30000000"))
    assert await _agg_totals(engine, "daily") == before
    assert await _agg_totals(engine, "hourly") == before


async def test_ingest_returns_503_when_write_queue_full(client, registered_app, wq):
    """A batch that cannot be queued is never acknowledged with 202."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.QUEUE_MAX", 0):
        r = await client.post("/api/v1/ingest", headers=_headers(registered_app), json={
            "batch_id": str(uuid.uuid4()), "records": [_rec("openai", "gpt-4o", "0.10")]})
    assert r.status_code == 503
    assert r.headers.get("Retry-After")


# ── Bug 3: sessions ──────────────────────────────────────────────────────────

async def test_ingest_carries_session_and_span_ids_to_attribution(client, registered_app, wq, engine):
    """Session ids from raw records AND aggregated traces are stored, and the
    attribution engine turns them into sessions served by the Sessions API."""
    from orchestrator.core import attribution_engine
    from orchestrator.db import session as session_mod

    past = NOW - timedelta(minutes=10)
    sid_raw, sid_agg = "sess-raw-" + uuid.uuid4().hex[:8], "sess-agg-" + uuid.uuid4().hex[:8]
    root = {"mds_session_id": sid_raw, "mds_call_id": "c1", "mds_parent_id": None,
            "mds_span_name": "plan", "mds_fw_tier": "custom"}
    child = {"mds_session_id": sid_raw, "mds_call_id": "c2", "mds_parent_id": "c1",
             "mds_span_name": "act", "mds_fw_tier": "custom"}
    raw = {"batch_id": str(uuid.uuid4()), "records": [
        _rec("openai", "gpt-4o", "0.40", ts=past, metadata=root),
        _rec("openai", "gpt-4o", "0.60", ts=past, metadata=child)]}
    agg = {"batch_id": str(uuid.uuid4()), "format": "aggregated",
           "traces_counted_in_aggregates": True,
           "aggregates": [_agg("anthropic", "claude-x", 2, "0.30", start=past.replace(minute=0, second=0, microsecond=0))],
           "traces": [_rec("anthropic", "claude-x", "0.10", ts=past, metadata={"mds_session_id": sid_agg, "mds_call_id": "a1"}),
                      _rec("anthropic", "claude-x", "0.20", ts=past, metadata={"mds_session_id": sid_agg, "mds_call_id": "a2", "mds_parent_id": "a1"})]}
    for p in (raw, agg):
        r = await client.post("/api/v1/ingest", json=p, headers=_headers(registered_app))
        assert r.status_code == 202, r.text
    await _drain(wq)

    factory = session_mod._session_factory
    async with factory() as db:
        sids = sorted({r for r in (await db.execute(select(UsageRecord.session_id))).scalars()})
    assert sids == sorted([sid_raw, sid_agg])

    with patch.object(attribution_engine, "_session_factory", factory):
        assert await attribution_engine.process_pending_sessions() == 2

    r = await client.get("/api/v1/attribution/sessions?hours=24")
    got = {s["session_id"]: s for s in r.json()}
    assert Decimal(got[sid_raw]["total_cost"]) == Decimal("1.00")
    assert got[sid_raw]["total_calls"] == 2
    assert Decimal(got[sid_agg]["total_cost"]) == Decimal("0.30")


async def test_ingest_rejects_unstorable_session_id(client, registered_app, wq):
    r = await client.post("/api/v1/ingest", headers=_headers(registered_app), json={
        "batch_id": str(uuid.uuid4()),
        "records": [_rec("openai", "gpt-4o", "0.1", metadata={"mds_session_id": "x" * 65})]})
    assert r.status_code == 422


async def test_attribution_not_starved_by_processed_sessions(engine, registered_app):
    """More than MAX_SESSIONS_PER_CYCLE processed sessions must not hide new ones."""
    from orchestrator.core import attribution_engine

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    past = NOW - timedelta(minutes=10)
    n = attribution_engine.MAX_SESSIONS_PER_CYCLE + 5
    async with factory() as db:
        for i in range(n):
            db.add(UsageRecord(
                app_id=registered_app["app_uuid"], team_id=registered_app["team_id"],
                provider="openai", model="gpt-4o", resource_type="llm_call",
                total_cost=Decimal("0.01"), timestamp=past, session_id=f"s-{i:03d}",
                metadata_={"mds_session_id": f"s-{i:03d}", "mds_call_id": f"c{i}"},
            ))
        await db.commit()
    with patch.object(attribution_engine, "_session_factory", factory):
        first = await attribution_engine.process_pending_sessions()
        second = await attribution_engine.process_pending_sessions()
    assert first == attribution_engine.MAX_SESSIONS_PER_CYCLE
    assert second == n - first
    async with factory() as db:
        assert (await db.execute(
            select(func.count()).select_from(AttributionSession)
        )).scalar_one() == n


# ── Bug 4: delta ─────────────────────────────────────────────────────────────

async def test_cost_delta_compares_prior_seven_days_only(client, db_session):
    team = Team(slug="delta", name="Delta")
    db_session.add(team)
    await db_session.flush()
    app = App(team_id=team.id, app_id="delta-app", app_name="D", api_key_hash="x", api_key_prefix="mds_delta")
    db_session.add(app)
    await db_session.flush()
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)

    def day_row(days_ago, cost):
        start = today - timedelta(days=days_ago)
        return UsageAggregate(
            app_id=app.id, team_id=team.id, provider="openai", model="gpt-4o",
            resource_type="llm_call", granularity="daily", period_start=start,
            period_end=start + timedelta(days=1), call_count=1, total_cost=Decimal(cost))

    for d in range(0, 7):      # current 7 days: 7 x $10
        db_session.add(day_row(d, "10"))
    for d in range(7, 14):     # prior 7 days: 7 x $5
        db_session.add(day_row(d, "5"))
    db_session.add(day_row(20, "1000"))  # outside both windows
    await db_session.commit()

    s = await _summary(client)
    assert Decimal(str(s["total_cost_7d"])) == Decimal("70")
    assert s["cost_delta_pct_7d"] == 100.0


# ── Bug 5: anomaly + forecast on SQLite ──────────────────────────────────────

async def test_anomaly_scan_and_forecast_run_on_sqlite(engine, registered_app):
    from orchestrator.core import insights_engine

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    async with factory() as db:
        for d, cost in enumerate(["9", "10", "11", "10", "9", "11", "10"], start=1):
            start = today - timedelta(days=d)
            db.add(UsageAggregate(
                app_id=registered_app["app_uuid"], team_id=registered_app["team_id"],
                provider="openai", model="gpt-4o", resource_type="llm_call",
                granularity="daily", period_start=start, period_end=start + timedelta(days=1),
                call_count=10, total_cost=Decimal(cost)))
        db.add(UsageAggregate(  # spike: $50 in the current hour
            app_id=registered_app["app_uuid"], team_id=registered_app["team_id"],
            provider="openai", model="gpt-4o", resource_type="llm_call",
            granularity="hourly", period_start=HOUR, period_end=HOUR + timedelta(hours=1),
            call_count=50, total_cost=Decimal("50")))
        await db.commit()

    from orchestrator.core.config import settings
    assert settings.is_sqlite
    with patch.object(insights_engine, "_session_factory", factory):
        await insights_engine.run_anomaly_scan()
        await insights_engine.run_forecast_update()

    async with factory() as db:
        events = (await db.execute(select(AnomalyEvent))).scalars().all()
        forecasts = (await db.execute(select(SpendForecast))).scalars().all()
    assert len(events) == 1
    assert events[0].app_id == registered_app["app_uuid"]
    assert float(events[0].z_score) >= 3.0
    assert len(forecasts) == 1
    assert Decimal(str(forecasts[0].mtd_actual)) >= Decimal("0")


# ── Bug 6: rate limiting ─────────────────────────────────────────────────────

async def test_sdk_traffic_is_not_throttled_at_normal_volume(client, registered_app, wq, monkeypatch):
    """600 SDK calls in a minute from one app pass; the dashboard-sized
    default class still limits other traffic and answers with Retry-After."""
    from orchestrator import main
    from orchestrator.core.config import settings

    monkeypatch.setattr(main, "_rate_limiter", main._RateLimiter(
        limit=settings.rate_limit_per_minute, burst=settings.rate_limit_burst))
    monkeypatch.setattr(main, "_sdk_rate_limiter", main._RateLimiter(
        limit=settings.rate_limit_sdk_per_minute, burst=settings.rate_limit_sdk_burst))

    statuses = []
    for i in range(600):
        if i % 2:
            r = await client.post("/api/v1/heartbeat", json={}, headers=_headers(registered_app))
        else:
            r = await client.post("/api/v1/ingest", headers=_headers(registered_app), json={
                "batch_id": f"burst-{i:04d}-{uuid.uuid4().hex[:8]}",
                "records": [_rec("openai", "gpt-4o", "0.01")]})
        statuses.append(r.status_code)
    await _drain(wq)
    assert 429 not in statuses
    assert set(statuses) <= {200, 202}

    limit = settings.rate_limit_per_minute + settings.rate_limit_burst
    seen_429 = None
    for _ in range(limit + 5):
        r = await client.get("/api/v1/dashboard/app-status")
        if r.status_code == 429:
            seen_429 = r
            break
    assert seen_429 is not None
    assert int(seen_429.headers["Retry-After"]) >= 1


def test_rate_limit_classes():
    from starlette.requests import Request

    from orchestrator.main import _rate_limit_class

    def req(method, path, headers=None):
        raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        return Request({"type": "http", "method": method, "path": path, "headers": raw,
                        "client": ("1.2.3.4", 1), "query_string": b""})

    key = {"X-Modus-APIKey": "mds_" + "a" * 40}
    for path in ("/api/v1/ingest", "/api/v1/heartbeat", "/api/v1/policy/evaluate"):
        assert _rate_limit_class(req("POST", path, key))[0] == "sdk"
    assert _rate_limit_class(req("GET", "/api/v1/policies", key))[0] == "sdk"
    # Without an app credential, or on other routes: default class
    assert _rate_limit_class(req("POST", "/api/v1/ingest"))[0] == "default"
    assert _rate_limit_class(req("GET", "/api/v1/dashboard/summary", key))[0] == "default"


# ── SDK: 429 handling, idempotent retries, sessions ──────────────────────────

def _agent(**kw):
    from modus.agent import ModusAgent
    a = ModusAgent(orchestrator_url="http://modus.test", team_token="mds_team_x",
                   app_id="a", environment="test", **kw)
    a._api_key = "mds_" + "k" * 40
    return a


def _http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return HTTPError("http://modus.test", code, "err", headers, BytesIO(b"{}"))


def _ok(body):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_sdk_evaluate_retries_429_and_keeps_breaker_closed():
    agent = _agent()
    calls = [_http_error(429, 0), _ok({"decision": "allow"})]
    with patch("modus.agent._urlopen_tls", side_effect=calls), patch("modus.agent.time.sleep"):
        result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")
    assert result == {"decision": "allow"}
    assert agent._gateway_consecutive_failures == 0
    assert agent._circuit_breaker_tripped_at is None


def test_sdk_persistent_429_keeps_enforcing_last_server_decision():
    from modus.agent import PolicyViolationError

    agent = _agent()
    deny = {"decision": "deny", "reason": "budget exceeded", "policy_name": "cap"}
    with patch("modus.agent._urlopen_tls", return_value=_ok(deny)):
        with pytest.raises(PolicyViolationError):
            agent.enforce("openai", "gpt-4o", bypass_cache=True)
    # Now the orchestrator rate limits every call: the SDK must NOT fail open
    with patch("modus.agent._urlopen_tls", side_effect=lambda *a, **k: (_ for _ in ()).throw(_http_error(429, 0))), \
         patch("modus.agent.time.sleep"):
        for _ in range(10):
            with pytest.raises(PolicyViolationError) as exc:
                agent.enforce("openai", "gpt-4o", bypass_cache=True)
            assert exc.value.decision == "deny"
    assert agent._circuit_breaker_tripped_at is None
    assert agent.rate_limited_count >= 10


def test_sdk_ingest_429_backs_off_and_retries_same_batch_id():
    agent = _agent(aggregation_enabled=True, trace_sample_rate=0.0)
    agent.record(provider="openai", model="gpt-4o", input_tokens=10, output_tokens=1,
                 input_cost=Decimal("0"), output_cost=Decimal("0.5"))
    sent = []

    def post_429(req, timeout=None):
        sent.append(json.loads(req.data)["batch_id"])
        raise _http_error(429, 7)

    with patch("modus.agent._urlopen_tls", side_effect=post_429):
        agent.flush()
        agent.flush()  # inside the backoff window: nothing is sent
    assert len(sent) == 1
    assert agent._ingest_backoff_until - __import__("time").monotonic() >= 6.5

    agent._ingest_backoff_until = 0.0

    def post_ok(req, timeout=None):
        sent.append(json.loads(req.data)["batch_id"])
        return _ok({"accepted": 1})

    with patch("modus.agent._urlopen_tls", side_effect=post_ok):
        agent.flush()
    assert sent[1] == sent[0]           # identical batch id → server dedup
    assert agent._pending_batches == []


def test_sdk_drops_invalid_batch_instead_of_retrying_forever():
    agent = _agent(aggregation_enabled=False)
    agent.record(provider="openai", model="gpt-4o", input_tokens=1, output_tokens=1)
    with patch("modus.agent._urlopen_tls", side_effect=_http_error(422)):
        agent.flush()
    assert agent._pending_batches == []


def test_sdk_record_in_session_is_counted_and_traced():
    agent = _agent(aggregation_enabled=True, trace_sample_rate=0.0)
    with agent.session("my-session"):
        with agent.span("plan"):
            agent.record(provider="openai", model="gpt-4o", input_tokens=10, output_tokens=1,
                         input_cost=Decimal("0"), output_cost=Decimal("0.25"))
    agent.record(provider="openai", model="gpt-4o", input_tokens=10, output_tokens=1,
                 input_cost=Decimal("0"), output_cost=Decimal("0.25"))
    assert sum(b.call_count for b in agent._agg_buckets.values()) == 2
    assert len(agent._agg_sampled) == 1
    meta = agent._agg_sampled[0].metadata
    assert meta["mds_session_id"] == "my-session"
    assert meta["mds_span_name"] == "plan" and meta["mds_call_id"]
    with pytest.raises(ValueError):
        agent.session("bad id with spaces")
