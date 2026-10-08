"""
Tests for the Modus Conductor layer.

Validates:
  - Conductor app starts and serves health endpoints
  - Orchestrator registration
  - Data push/ack protocol
  - Dashboard API endpoint contracts
  - Data completeness tracking
  - Tiered cache behaviour
"""

from __future__ import annotations

import os
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from httpx import ASGITransport, AsyncClient

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from conductor.db.models import Base as ConductorBase
from conductor.db import session as conductor_session
import conductor.core.config as conductor_config
from conductor.main import create_app

# Test conductor secret — set in env and used in auth headers
_TEST_CONDUCTOR_SECRET = "test-conductor-secret"
CONDUCTOR_AUTH = {"Authorization": f"Bearer {_TEST_CONDUCTOR_SECRET}"}


@pytest_asyncio.fixture
async def conductor_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(ConductorBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def conductor_db(conductor_engine):
    factory = async_sessionmaker(conductor_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def conductor_client(conductor_engine):
    factory = async_sessionmaker(conductor_engine, class_=AsyncSession, expire_on_commit=False)

    # Ensure the conductor secret is set and propagated to ALL modules.
    # The settings singleton is created at import time and cached. Other test
    # modules may import conductor before we set the env var. We must:
    # 1. Set the env var
    # 2. Clear the lru_cache
    # 3. Replace the module-level settings reference in config
    # 4. Replace the module-level settings reference in auth (which imported it)
    os.environ["CONDUCTOR_CONDUCTOR_SECRET"] = _TEST_CONDUCTOR_SECRET
    conductor_config.get_settings.cache_clear()
    fresh_settings = conductor_config.get_settings()
    conductor_config.settings = fresh_settings
    # Patch auth module's reference too — it imported settings from config at load time
    import conductor.core.auth as conductor_auth
    conductor_auth.settings = fresh_settings

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Override AFTER entering the client context so the lifespan's init_db()
        # has already run. This ensures our in-memory test DB is used instead of
        # the default file-based one.
        conductor_session._engine = conductor_engine
        conductor_session._session_factory = factory
        yield client


# ── Health Checks ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz(conductor_client):
    resp = await conductor_client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "conductor"


@pytest.mark.asyncio
async def test_readiness(conductor_client):
    resp = await conductor_client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["orchestrators_registered"] == 0


# ── Orchestrator Registration ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_orchestrator(conductor_client):
    resp = await conductor_client.post(
        "/api/v1/conductor/register",
        json={
            "name": "payments-api-prod",
            "instance_id": "orch-001",
            "endpoint_url": "http://localhost:8080",
            "version": "1.0.0",
            "environment": "production",
            "app_count": 3,
            "agent_count": 5,
            "region_id": "us-east-1",
        },
        headers={**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "orch-001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "registered"
    assert data["instance_id"] == "orch-001"
    assert data["name"] == "payments-api-prod"


@pytest.mark.asyncio
async def test_register_orchestrator_idempotent(conductor_client):
    headers = {**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "orch-002"}
    body = {
        "name": "billing-api",
        "instance_id": "orch-002",
        "app_count": 1,
        "agent_count": 2,
    }
    resp1 = await conductor_client.post("/api/v1/conductor/register", json=body, headers=headers)
    assert resp1.json()["status"] == "registered"

    resp2 = await conductor_client.post("/api/v1/conductor/register", json=body, headers=headers)
    assert resp2.json()["status"] == "updated"
    assert resp2.json()["orchestrator_node_id"] == resp1.json()["orchestrator_node_id"]


@pytest.mark.asyncio
async def test_list_orchestrators(conductor_client):
    # Register two
    for i, name in enumerate(["app-a", "app-b"]):
        await conductor_client.post(
            "/api/v1/conductor/register",
            json={"name": name, "instance_id": f"list-{i}", "app_count": i + 1, "agent_count": i + 1},
            headers={**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": f"list-{i}"},
        )

    resp = await conductor_client.get("/api/v1/conductor/orchestrators")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2


# ── Data Push/Ack Protocol ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_data(conductor_client):
    # Register first
    await conductor_client.post(
        "/api/v1/conductor/register",
        json={"name": "push-test", "instance_id": "push-001", "app_count": 1, "agent_count": 1},
        headers={**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "push-001"},
    )

    now = datetime.now(timezone.utc)
    resp = await conductor_client.post(
        "/api/v1/conductor/push",
        json={
            "batch_id": "batch-test-001",
            "aggregates": [
                {
                    "app_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
                    "app_name": "my-app",
                    "team_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
                    "team_slug": "engineering",
                    "team_name": "Engineering",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "granularity": "daily",
                    "period_start": now.replace(hour=0, minute=0, second=0).isoformat(),
                    "period_end": (now.replace(hour=0, minute=0, second=0) + timedelta(days=1)).isoformat(),
                    "call_count": 150,
                    "input_tokens": 500000,
                    "output_tokens": 200000,
                    "total_tokens": 700000,
                    "total_cost": "12.50",
                },
            ],
            "teams": [
                {
                    "id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
                    "slug": "engineering",
                    "name": "Engineering",
                    "budget_monthly_usd": "10000.00",
                },
            ],
            "apps": [
                {
                    "id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
                    "app_id": "my-app",
                    "app_name": "My App",
                    "team_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
                    "is_active": True,
                    "last_seen_at": now.isoformat(),
                },
            ],
            "alerts": [],
            "policy_decisions": [],
        },
        headers={**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "push-001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["aggregates_accepted"] == 1
    assert data["teams_updated"] == 1
    assert data["apps_updated"] == 1


@pytest.mark.asyncio
async def test_push_duplicate_rejected(conductor_client):
    # Register
    await conductor_client.post(
        "/api/v1/conductor/register",
        json={"name": "dedup-test", "instance_id": "dedup-001", "app_count": 1, "agent_count": 1},
        headers={**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "dedup-001"},
    )

    payload = {
        "batch_id": "batch-dedup-001",
        "aggregates": [],
        "teams": [],
        "apps": [],
        "alerts": [],
        "policy_decisions": [],
    }
    headers = {**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "dedup-001"}

    resp1 = await conductor_client.post("/api/v1/conductor/push", json=payload, headers=headers)
    assert resp1.json()["status"] == "accepted"

    resp2 = await conductor_client.post("/api/v1/conductor/push", json=payload, headers=headers)
    assert resp2.json()["status"] == "duplicate"


# ── Dashboard API Endpoints ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_summary(conductor_client):
    # Push data first
    await conductor_client.post(
        "/api/v1/conductor/register",
        json={"name": "dash-test", "instance_id": "dash-001", "app_count": 1, "agent_count": 1},
        headers={**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "dash-001"},
    )

    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await conductor_client.post(
        "/api/v1/conductor/push",
        json={
            "batch_id": "batch-dash-001",
            "aggregates": [
                {
                    "app_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "app_name": "dash-app",
                    "team_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "team_slug": "platform",
                    "team_name": "Platform",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "granularity": "daily",
                    "period_start": today.isoformat(),
                    "period_end": (today + timedelta(days=1)).isoformat(),
                    "call_count": 100,
                    "input_tokens": 300000,
                    "output_tokens": 100000,
                    "total_tokens": 400000,
                    "total_cost": "8.75",
                },
            ],
            "teams": [{"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "slug": "platform", "name": "Platform"}],
            "apps": [
                {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "app_id": "dash-app",
                 "app_name": "Dash App", "team_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                 "is_active": True, "last_seen_at": now.isoformat()},
            ],
            "alerts": [],
            "policy_decisions": [],
        },
        headers={**CONDUCTOR_AUTH, "X-Orchestrator-Instance-ID": "dash-001"},
    )

    # Now query the dashboard summary
    resp = await conductor_client.get("/api/v1/dashboard/summary")
    assert resp.status_code == 200
    data = resp.json()

    # Verify KPI fields exist and have correct types
    assert "total_cost_today" in data
    assert "total_cost_7d" in data
    assert "total_cost_30d" in data
    assert "total_cost_mtd" in data
    assert "active_apps" in data
    assert "online_agents" in data
    assert "active_teams" in data
    assert "data_completeness_pct" in data  # Conductor-specific

    # Verify cost is positive (we just pushed data)
    assert float(data["total_cost_today"]) >= 8.75


@pytest.mark.asyncio
async def test_dashboard_cost_over_time(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/cost-over-time?days=7")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_dashboard_by_provider(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/by-provider?days=30")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_dashboard_by_app(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/by-app?days=30")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_dashboard_by_team(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/by-team?days=30")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_dashboard_top_models(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/top-models?days=30")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_dashboard_recent_alerts(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/recent-alerts")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_dashboard_app_status(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/app-status")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_dashboard_executive_charts(conductor_client):
    resp = await conductor_client.get("/api/v1/dashboard/executive-charts?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert "savings_over_time" in data
    assert "enforcement_mix" in data
    assert "provider_allocation" in data


# ── Cache Behaviour ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit():
    from conductor.core.cache import TieredCache, CacheTier

    c = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
    await c.set("test-key", {"value": 42}, CacheTier.HOT)
    result = await c.get("test-key")
    assert result == {"value": 42}
    assert c.stats["hits"] == 1


@pytest.mark.asyncio
async def test_cache_invalidation():
    from conductor.core.cache import TieredCache, CacheTier

    c = TieredCache()
    await c.set("key-a", "a", CacheTier.HOT)
    await c.set("key-b", "b", CacheTier.WARM)
    await c.invalidate_tier(CacheTier.HOT)
    assert await c.get("key-a") is None
    assert await c.get("key-b") == "b"
