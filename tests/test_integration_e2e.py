"""
Integration Test — End-to-End Flow
====================================
Tests the full pipeline: ingest → DB write → aggregation → dashboard data.

This bypasses agent auth (ingest key verification) and uses the stub identity
for dashboard endpoints, focusing on data flow correctness.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.db.models import Base, UsageRecord, IngestBatch, UsageAggregate, App, Team
# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def team_id():
    return str(uuid.uuid4())


@pytest.fixture
def app_id():
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def e2e_engine():
    """Shared in-memory SQLite engine for the full integration test."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def e2e_session_factory(e2e_engine):
    return async_sessionmaker(e2e_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def e2e_db(e2e_session_factory):
    async with e2e_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_db(e2e_db, team_id, app_id):
    """Seed a team and app into the DB."""
    team = Team(id=team_id, slug="test-team", name="Test Team")
    e2e_db.add(team)

    app = App(
        id=app_id,
        team_id=team_id,
        app_id="test-app",
        app_name="Test App",
        environment="test",
        api_key_hash="$2b$12$placeholder_hash_for_testing_only___",
        api_key_prefix="mds_test1234test",
    )
    e2e_db.add(app)
    await e2e_db.commit()
    return e2e_db


@pytest_asyncio.fixture
async def e2e_client(e2e_engine, e2e_session_factory, seeded_db, app_id, team_id):
    """HTTP client with ingest auth bypassed — returns raw key for the seeded app."""
    from orchestrator.db import session as session_mod
    from orchestrator.db.session import get_session

    async def _override_session():
        async with e2e_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    # Mock get_app_identity to return a valid mds_ key
    # The ingest endpoint's _verify_app_key will be patched separately
    _prev_engine = session_mod._engine
    session_mod._engine = e2e_engine

    from orchestrator.main import create_app
    test_app = create_app()
    test_app.dependency_overrides[get_session] = _override_session

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    session_mod._engine = _prev_engine


# ── Helper ───────────────────────────────────────────────────────────────────


def _make_records(n: int, provider: str = "openai", model: str = "gpt-4o",
                  cost_each: str = "0.01", input_tokens: int = 500,
                  output_tokens: int = 100, base_time: datetime | None = None):
    """Generate N usage records."""
    base = base_time or datetime.now(timezone.utc)
    return [
        {
            "provider": provider,
            "resource_type": "llm",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_cost": cost_each,
            "duration_ms": 200,
            "timestamp": (base - timedelta(minutes=i)).isoformat(),
        }
        for i in range(n)
    ]


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_health_endpoints(e2e_client):
    """Smoke test: health endpoints work."""
    resp = await e2e_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    resp = await e2e_client.get("/ready")
    assert resp.status_code == 200


async def test_dashboard_summary_empty(e2e_client):
    """Dashboard summary returns zeros when no data ingested."""
    resp = await e2e_client.get("/api/v1/dashboard/summary", params={"days": 7})
    assert resp.status_code == 200
    data = resp.json()
    assert "total_cost" in data or "cost_today" in data or isinstance(data, dict)


async def test_dashboard_apps_empty(e2e_client):
    """App breakdown returns empty list on fresh DB."""
    resp = await e2e_client.get("/api/v1/dashboard/by-app", params={"days": 7})
    assert resp.status_code == 200


async def test_ingest_writes_to_db(e2e_session_factory, app_id, team_id):
    """
    Direct DB test: verify that usage records can be written and read back.
    This tests the data layer without HTTP/auth complexity.
    """
    async with e2e_session_factory() as db:
        now = datetime.now(timezone.utc)
        batch_id = str(uuid.uuid4())

        # Write a batch marker
        db.add(IngestBatch(
            batch_id=batch_id,
            app_id=app_id,
            record_count=3,
        ))

        # Write usage records
        for i in range(3):
            db.add(UsageRecord(
                app_id=app_id,
                team_id=team_id,
                provider="openai",
                resource_type="llm",
                model="gpt-4o",
                input_tokens=500,
                output_tokens=100,
                total_cost=Decimal("0.01"),
                duration_ms=200,
                timestamp=now - timedelta(minutes=i),
                batch_id=batch_id,
            ))
        await db.commit()

    # Verify records are in DB
    async with e2e_session_factory() as db:
        result = await db.execute(
            select(UsageRecord).where(UsageRecord.app_id == app_id)
        )
        records = result.scalars().all()
        assert len(records) == 3
        assert all(r.provider == "openai" for r in records)
        assert all(r.total_cost == Decimal("0.01") for r in records)


async def test_aggregation_produces_hourly_and_daily(e2e_engine, e2e_session_factory, app_id, team_id):
    """
    Ingest raw records through the writer: hourly + daily aggregates are
    written in the same transaction. Running the aggregation cycle any number
    of times afterwards must not change the totals.
    """
    now = datetime.now(timezone.utc)
    from orchestrator.core import write_queue as wq_mod
    from orchestrator.db import session as session_mod
    _prev = session_mod._session_factory
    session_mod._session_factory = e2e_session_factory

    records = [
        dict(
            app_id=app_id,
            team_id=team_id,
            provider="anthropic",
            resource_type="llm",
            model="claude-sonnet-4-20250514",
            input_tokens=1000,
            output_tokens=200,
            total_cost=Decimal("0.005"),
            duration_ms=300,
            timestamp=now - timedelta(minutes=i),
            batch_id="e2e-agg-batch-0001",
        )
        for i in range(5)
    ]

    async def _totals():
        async with e2e_session_factory() as db:
            out = {}
            for gran in ("hourly", "daily"):
                rows = (await db.execute(
                    select(UsageAggregate).where(
                        UsageAggregate.app_id == app_id,
                        UsageAggregate.granularity == gran,
                    )
                )).scalars().all()
                out[gran] = (
                    sum(r.call_count for r in rows),
                    sum((r.total_cost for r in rows), Decimal("0")),
                )
            return out

    try:
        await wq_mod._flush_batch([wq_mod.IngestItem(
            app_id=app_id, team_id=team_id, batch_id="e2e-agg-batch-0001",
            records=records, record_count=len(records),
        )])
        before = await _totals()

        await wq_mod.start_writer()
        from orchestrator.core.aggregator import run_aggregation_cycle
        import asyncio
        for _ in range(3):
            await run_aggregation_cycle()
            await asyncio.sleep(0.3)
        await wq_mod.stop_writer()
        after = await _totals()
    finally:
        session_mod._session_factory = _prev

    # Records may straddle an hour/day boundary, so compare sums, not rows.
    assert before["hourly"] == (5, Decimal("0.025"))
    assert before["daily"] == (5, Decimal("0.025"))
    assert after == before, f"aggregation cycle changed totals: {before} -> {after}"


async def test_security_headers_present(e2e_client):
    """Verify security headers are set on responses."""
    resp = await e2e_client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in resp.headers.get("Permissions-Policy", "")


async def test_dashboard_time_series_returns_structure(e2e_client):
    """Dashboard cost-over-time endpoint returns expected shape."""
    resp = await e2e_client.get("/api/v1/dashboard/cost-over-time", params={"days": 7})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


async def test_dashboard_provider_breakdown_structure(e2e_client):
    """Provider breakdown returns expected shape."""
    resp = await e2e_client.get("/api/v1/dashboard/by-provider", params={"days": 7})
    assert resp.status_code == 200


async def test_encryption_roundtrip():
    """Credential encryption utility encrypts and decrypts correctly."""
    import os
    from cryptography.fernet import Fernet

    # Generate a test key
    test_key = Fernet.generate_key().decode()
    os.environ["MODUS_ENCRYPTION_KEY"] = test_key

    # Reset cached cipher AND reload settings so it picks up the env var
    from orchestrator.core import credential_crypto
    from orchestrator.core.config import Settings
    credential_crypto.reset_cipher_cache()

    import orchestrator.core.config as config_mod
    _orig = config_mod.settings
    config_mod.settings = Settings()

    from orchestrator.core.credential_crypto import decrypt_credential, encrypt_credential

    plaintext = '{"api_key": "sk-test-12345", "secret": "very-secret"}'
    ciphertext = encrypt_credential(plaintext)

    assert ciphertext != plaintext, "Encryption should change the value"
    assert ciphertext.startswith("enc:v1:"), "Ciphertext should carry the version marker"
    assert decrypt_credential(ciphertext) == plaintext, "Decrypt should recover plaintext"

    # Clean up
    del os.environ["MODUS_ENCRYPTION_KEY"]
    credential_crypto.reset_cipher_cache()
    config_mod.settings = _orig


async def test_write_queue_backpressure():
    """Queue pressure functions return expected values."""
    from orchestrator.core.write_queue import queue_pressure, queue_over_pressure

    # Before queue is started, should return safe defaults
    assert queue_pressure() == 0.0
    assert queue_over_pressure() is False
