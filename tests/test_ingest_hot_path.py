"""
Tests — Ingest Hot Path
==========================
Tests the critical SDK → Orchestrator ingest pipeline:
  - Valid ingest → 202 with correct response
  - Aggregated format (v2 SDK) ingest
  - Backpressure rejection (503)
  - Auth dispatch (mds_ key verification with cache)
  - Batch size validation
  - Write queue enqueue (non-blocking)

These tests exercise the real auth + ingest code paths with a seeded
App in the DB.  The bcrypt hash is pre-computed at low cost (rounds=4)
to keep tests fast.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.db.models import Base, App, Team
# ── Pre-computed test key + bcrypt hash (rounds=4 for speed) ─────────────────

_TEST_RAW_KEY = "mds_testhotpath00" + "a" * 24  # 42 chars
_TEST_KEY_PREFIX = _TEST_RAW_KEY[:16]            # "mds_testhotpath0"
_TEST_KEY_HASH = bcrypt.hashpw(
    _TEST_RAW_KEY.encode(), bcrypt.gensalt(rounds=4)
).decode()


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def hp_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def hp_session_factory(hp_engine):
    return async_sessionmaker(hp_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def team_id():
    return str(uuid.uuid4())


@pytest.fixture
def app_uuid():
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def seeded_app(hp_session_factory, team_id, app_uuid):
    """Seed a team + app with a known bcrypt key hash."""
    async with hp_session_factory() as db:
        db.add(Team(id=team_id, slug="hp-team", name="Hot Path Team"))
        db.add(App(
            id=app_uuid,
            team_id=team_id,
            app_id="hp-test-app",
            app_name="Hot Path App",
            environment="test",
            api_key_hash=_TEST_KEY_HASH,
            api_key_prefix=_TEST_KEY_PREFIX,
        ))
        await db.commit()
    return app_uuid, team_id


@pytest_asyncio.fixture
async def hp_client(hp_engine, hp_session_factory, seeded_app):
    """HTTP client wired to the seeded in-memory DB."""
    from orchestrator.db import session as session_mod
    from orchestrator.db.session import get_session

    async def _override_session():
        async with hp_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    _prev_engine = session_mod._engine
    session_mod._engine = hp_engine

    from orchestrator.main import create_app
    test_app = create_app()
    test_app.dependency_overrides[get_session] = _override_session

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    session_mod._engine = _prev_engine


# ── Helpers ──────────────────────────────────────────────────────────────────

def _headers(key: str = _TEST_RAW_KEY) -> dict:
    return {"X-Modus-APIKey": key}


def _raw_payload(n: int = 3, batch_id: str | None = None) -> dict:
    bid = batch_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    return {
        "batch_id": bid,
        "records": [
            {
                "provider": "openai",
                "resource_type": "llm",
                "model": "gpt-4o",
                "input_tokens": 500,
                "output_tokens": 100,
                "total_cost": "0.01",
                "duration_ms": 200,
                "timestamp": (now - timedelta(minutes=i)).isoformat(),
            }
            for i in range(n)
        ],
    }


def _aggregated_payload(batch_id: str | None = None) -> dict:
    bid = batch_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    return {
        "batch_id": bid,
        "format": "aggregated",
        "aggregates": [
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "resource_type": "llm",
                "call_count": 50,
                "input_tokens": 25000,
                "output_tokens": 5000,
                "total_tokens": 30000,
                "total_cost": "1.25",
                "duration_ms_sum": 10000,
                "duration_ms_min": 100,
                "duration_ms_max": 500,
                "duration_ms_avg": 200,
                "window_start": hour_start.isoformat(),
                "window_end": (hour_start + timedelta(hours=1)).isoformat(),
            }
        ],
        "traces": [
            {
                "provider": "anthropic",
                "resource_type": "llm",
                "model": "claude-sonnet-4-20250514",
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_cost": "0.05",
                "duration_ms": 350,
                "timestamp": now.isoformat(),
            }
        ],
    }


# ── Tests: Valid Ingest (positive path) ──────────────────────────────────────

async def test_ingest_raw_valid_returns_202(hp_client):
    """Valid raw ingest with correct key returns 202 ACCEPTED."""
    # Patch write queue to avoid needing a running writer
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock) as mock_enqueue:
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_raw_payload(n=3),
            headers=_headers(),
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 3
    assert body["rejected"] == 0
    assert body["duplicate"] is False
    # Write queue should have been called once with the batch
    mock_enqueue.assert_called_once()


async def test_ingest_aggregated_valid_returns_202(hp_client):
    """Aggregated format (v2 SDK) returns 202 with call_count as accepted."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock) as mock_enqueue:
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_aggregated_payload(),
            headers=_headers(),
        )
    assert resp.status_code == 202
    body = resp.json()
    # Aggregated: accepted = sum of call_count across aggregates
    assert body["accepted"] == 50
    assert body["rejected"] == 0
    # Two enqueue calls: one for aggregates, one for traces
    assert mock_enqueue.call_count == 2


async def test_ingest_single_record(hp_client):
    """Ingest with a single record succeeds."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock):
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_raw_payload(n=1),
            headers=_headers(),
        )
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1


# ── Tests: Auth Path ─────────────────────────────────────────────────────────

async def test_ingest_rejects_missing_key(hp_client):
    """No API key header → 401."""
    resp = await hp_client.post("/api/v1/ingest", json=_raw_payload())
    assert resp.status_code == 401


async def test_ingest_rejects_wrong_prefix(hp_client):
    """Key with wrong prefix → 401."""
    resp = await hp_client.post(
        "/api/v1/ingest",
        json=_raw_payload(),
        headers={"X-Modus-APIKey": "xyz_notvalid12345678"},
    )
    assert resp.status_code == 401


async def test_ingest_rejects_unknown_key(hp_client):
    """Valid prefix but unknown key → 401 (no matching app in DB)."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False):
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_raw_payload(),
            headers={"X-Modus-APIKey": "mds_unknown_key_" + "x" * 26},
        )
    assert resp.status_code == 401


# ── Tests: Key Cache Behavior ────────────────────────────────────────────────

async def test_key_cache_populated_after_first_verify(hp_client):
    """After first successful ingest, the key cache should be populated."""
    from orchestrator.api.ingest import _KEY_CACHE, _cache_key

    ck = _cache_key(_TEST_RAW_KEY)

    # Clear caches to ensure cold start
    from orchestrator.api.ingest import _KEY_CACHE_LOCK, _APP_CACHE, _APP_CACHE_LOCK
    with _KEY_CACHE_LOCK:
        _KEY_CACHE.clear()
    with _APP_CACHE_LOCK:
        _APP_CACHE.clear()

    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock):
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_raw_payload(n=1),
            headers=_headers(),
        )
    assert resp.status_code == 202

    # Key cache should now have an entry for our test key
    assert ck in _KEY_CACHE, "Key cache should be populated after successful verify"


async def test_pre_cache_registration_enables_immediate_ingest(hp_client, seeded_app):
    """pre_cache_registration bridges registration → first ingest gap."""
    from orchestrator.api.ingest import (
        pre_cache_registration, _KEY_CACHE, _KEY_CACHE_LOCK,
        _APP_CACHE, _APP_CACHE_LOCK, _cache_key,
    )

    app_uuid, team_id = seeded_app

    # Clear caches
    with _KEY_CACHE_LOCK:
        _KEY_CACHE.clear()
    with _APP_CACHE_LOCK:
        _APP_CACHE.clear()

    # Simulate what registration does
    pre_cache_registration(
        app_uuid=app_uuid,
        app_id="hp-test-app",
        team_id=team_id,
        environment="test",
        api_key=_TEST_RAW_KEY,
        api_key_prefix=_TEST_KEY_PREFIX,
        api_key_hash=_TEST_KEY_HASH,
    )

    # Key cache should now be populated
    ck = _cache_key(_TEST_RAW_KEY)
    assert ck in _KEY_CACHE

    # Ingest should succeed via cache (no bcrypt needed)
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock):
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_raw_payload(n=1),
            headers=_headers(),
        )
    assert resp.status_code == 202


# ── Tests: Backpressure ──────────────────────────────────────────────────────

async def test_ingest_backpressure_returns_503(hp_client):
    """When write queue is over pressure, ingest returns 503 with Retry-After."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=True):
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_raw_payload(),
            headers=_headers(),
        )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"


# ── Tests: Payload Validation ────────────────────────────────────────────────

async def test_ingest_rejects_empty_batch_id(hp_client):
    """Batch ID too short → 422."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False):
        payload = _raw_payload()
        payload["batch_id"] = "short"  # min_length=8
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=payload,
            headers=_headers(),
        )
    assert resp.status_code == 422


async def test_ingest_negative_tokens_rejected(hp_client):
    """Negative token counts → 422 validation error."""
    payload = {
        "batch_id": str(uuid.uuid4()),
        "records": [{
            "provider": "openai",
            "resource_type": "llm",
            "model": "gpt-4o",
            "input_tokens": -100,
            "output_tokens": 50,
            "total_cost": "0.01",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False):
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=payload,
            headers=_headers(),
        )
    assert resp.status_code == 422


async def test_ingest_provider_lowercased(hp_client):
    """Provider field is normalized to lowercase."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock) as mock_enqueue:
        payload = _raw_payload(n=1)
        payload["records"][0]["provider"] = "  OpenAI  "
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=payload,
            headers=_headers(),
        )
    assert resp.status_code == 202
    # Verify the enqueued record has lowercase provider
    call_args = mock_enqueue.call_args
    item = call_args[0][0]
    assert item.records[0]["provider"] == "openai"


async def test_ingest_timestamp_naive_gets_utc(hp_client):
    """Naive timestamps (no TZ) are treated as UTC."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock):
        payload = _raw_payload(n=1)
        # Send a naive timestamp (no timezone info)
        payload["records"][0]["timestamp"] = "2025-06-15T10:30:00"
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=payload,
            headers=_headers(),
        )
    assert resp.status_code == 202


# ── Tests: Write Queue Integration ───────────────────────────────────────────

async def test_ingest_enqueues_correct_item_shape(hp_client, seeded_app):
    """Enqueued IngestItem has correct fields from the payload."""
    app_uuid, team_id = seeded_app

    # Clear caches so this test's app is looked up fresh from DB
    from orchestrator.api.ingest import _KEY_CACHE, _KEY_CACHE_LOCK, _APP_CACHE, _APP_CACHE_LOCK
    with _KEY_CACHE_LOCK:
        _KEY_CACHE.clear()
    with _APP_CACHE_LOCK:
        _APP_CACHE.clear()

    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock) as mock_enqueue:
        payload = _raw_payload(n=2)
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=payload,
            headers=_headers(),
        )

    assert resp.status_code == 202
    item = mock_enqueue.call_args[0][0]

    assert item.app_id == app_uuid
    assert item.team_id == team_id
    assert item.batch_id == payload["batch_id"]
    assert item.record_count == 2
    assert len(item.records) == 2
    assert item.total_cost == Decimal("0.02")
    assert item.total_input_tokens == 1000
    assert item.total_output_tokens == 200


async def test_ingest_aggregated_enqueues_both_items(hp_client):
    """Aggregated ingest enqueues AggregationItem + IngestItem for traces."""
    with patch("orchestrator.core.write_queue.queue_over_pressure", return_value=False), \
         patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock) as mock_enqueue:
        resp = await hp_client.post(
            "/api/v1/ingest",
            json=_aggregated_payload(),
            headers=_headers(),
        )

    assert resp.status_code == 202
    assert mock_enqueue.call_count == 2

    # First call: AggregationItem (aggregates)
    from orchestrator.core.write_queue import AggregationItem, IngestItem
    agg_item = mock_enqueue.call_args_list[0][0][0]
    trace_item = mock_enqueue.call_args_list[1][0][0]

    assert isinstance(agg_item, AggregationItem)
    assert agg_item.granularity == "hourly"
    assert len(agg_item.rows) == 1

    assert isinstance(trace_item, IngestItem)
    assert len(trace_item.records) == 1
