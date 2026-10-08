"""
Broad, high-yield tests across:

  - orchestrator.core.conductor_push     full register + push flows (DB-backed)
  - orchestrator.core.maintenance        prune_batch_ids, compaction, partitions
  - orchestrator.core.connection_checker deeper gather + test paths
  - orchestrator.core.threshold_evaluator notification dispatch deep paths
  - orchestrator.core.governance_loop    detection rules with real seeded data
  - orchestrator.api.notifications       config + test endpoints
  - orchestrator.api.topology            self-register success paths
  - orchestrator.api.roles               assignments + permissions catalog
  - orchestrator.api.gateway             _get_stream_budget_limit (recently fixed)

These tests use real fixtures (db_session, client, engine) where possible
to exercise actual code paths rather than mocks.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _new_team_app(db_session, *, app_id_slug: str = "test-app", team_slug: str = "test-team") -> tuple[str, str]:
    """Sync-style factory used inside an awaited block to seed team+app."""
    from orchestrator.db.models import App, Team
    team_id = str(uuid.uuid4())
    app_uuid = str(uuid.uuid4())
    raw_key = f"mds_{uuid.uuid4().hex[:16]}"
    team = Team(id=team_id, slug=team_slug, name="Seeded Team")
    app = App(
        id=app_uuid,
        team_id=team_id,
        app_id=app_id_slug,
        app_name="Seeded App",
        environment="production",
        api_key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
        api_key_prefix=raw_key[:16],
        is_active=True,
    )
    db_session.add_all([team, app])
    return team_id, app_uuid


# ════════════════════════════════════════════════════════════════════════════
# 1. CONDUCTOR PUSH — full DB-backed register + push exercises lots of branches
# ════════════════════════════════════════════════════════════════════════════


class TestConductorPushDeep:
    async def test_register_with_conductor_success(self, engine):
        """Exercise register_with_conductor with seeded apps and a 200 response."""
        import orchestrator.core.conductor_push as cp
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import AgentHeartbeat, App, Team

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Seed 2 apps + 1 heartbeat to exercise the count queries
        team_id = str(uuid.uuid4())
        async with factory() as db:
            db.add(Team(id=team_id, slug="cp-team", name="CP Team"))
            await db.flush()
            app_a = App(
                team_id=team_id, app_id="app-a", app_name="App A",
                api_key_hash="x", api_key_prefix="mds_a", is_active=True,
            )
            app_b = App(
                team_id=team_id, app_id="app-b", app_name="App B",
                api_key_hash="y", api_key_prefix="mds_b", is_active=True,
            )
            db.add_all([app_a, app_b])
            await db.flush()
            db.add(AgentHeartbeat(
                app_id=str(app_a.id), team_id=team_id, agent_version="1.0",
            ))
            await db.commit()

        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json = MagicMock(return_value={
                "status": "ok",
                "orchestrator_node_id": "node-1",
            })
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch.object(cp.settings, "conductor_url", "http://conductor:8000"):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    result = await cp.register_with_conductor()
                    assert result is True
                    mock_client.post.assert_awaited_once()
        finally:
            session_mod._session_factory = prev_factory

    async def test_register_with_conductor_non_2xx(self, engine):
        """register returns False when the conductor responds with 500."""
        import orchestrator.core.conductor_push as cp
        from orchestrator.db import session as session_mod

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "boom"
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch.object(cp.settings, "conductor_url", "http://conductor:8000"):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    result = await cp.register_with_conductor()
                    assert result is False
        finally:
            session_mod._session_factory = prev_factory

    async def test_register_with_conductor_exception(self, engine):
        """register returns False and logs when an exception is raised."""
        import orchestrator.core.conductor_push as cp
        from orchestrator.db import session as session_mod

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        try:
            with patch.object(cp.settings, "conductor_url", "http://conductor:8000"):
                with patch("httpx.AsyncClient", side_effect=Exception("network down")):
                    result = await cp.register_with_conductor()
                    assert result is False
        finally:
            session_mod._session_factory = prev_factory

    async def test_push_to_conductor_full_path(self, engine):
        """Push flow: seed apps/teams/aggregates/alerts/policy decisions then push."""
        import orchestrator.core.conductor_push as cp
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import (
            Alert,
            App,
            Team,
            Threshold,
            UsageAggregate,
        )

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        threshold_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async with factory() as db:
            db.add(Team(id=team_id, slug="push-team", name="Push Team"))
            await db.flush()
            db.add(App(
                id=app_uuid, team_id=team_id, app_id="push-app",
                app_name="Push App", api_key_hash="x", api_key_prefix="mds_p",
                is_active=True, environment="production",
                last_seen_at=now,
            ))
            await db.flush()
            # Aggregate inside current month
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            db.add(UsageAggregate(
                app_id=app_uuid, team_id=team_id,
                provider="openai", model="gpt-4o", resource_type="llm_call",
                granularity="daily",
                period_start=month_start,
                period_end=month_start + timedelta(days=1),
                call_count=10, input_tokens=100, output_tokens=50,
                total_tokens=150,
                input_cost=Decimal("0.10"),
                output_cost=Decimal("0.20"),
                total_cost=Decimal("0.30"),
                avg_duration_ms=120,
                p95_duration_ms=200,
            ))
            db.add(Threshold(
                id=threshold_id, team_id=team_id, app_id=app_uuid,
                name="t1", scope="app", metric="total_cost",
                period="daily", critical_value=Decimal("100"), is_active=True,
            ))
            await db.flush()
            db.add(Alert(
                threshold_id=threshold_id, team_id=team_id, app_id=app_uuid,
                severity="warning", metric="total_cost",
                threshold_value=Decimal("100"), actual_value=Decimal("110"),
                period_start=month_start, period_end=month_start + timedelta(days=1),
                fired_at=now - timedelta(hours=1),
            ))
            await db.commit()

        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        cp._consecutive_failures = 0
        cp._circuit_open_until = None
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json = MagicMock(return_value={
                "status": "ok",
                "aggregates_accepted": 1,
                "teams_updated": 1,
                "apps_updated": 1,
                "alerts_accepted": 1,
            })
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch.object(cp.settings, "conductor_url", "http://conductor:8000"):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    result = await cp.push_to_conductor()
                    assert result is True
                    assert cp._consecutive_failures == 0
        finally:
            session_mod._session_factory = prev_factory
            cp._consecutive_failures = 0
            cp._circuit_open_until = None

    async def test_push_to_conductor_5xx_increments_failure(self, engine):
        """5xx response should increment _consecutive_failures."""
        import orchestrator.core.conductor_push as cp
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import Team

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            db.add(Team(slug="x5-team", name="X5 Team"))
            await db.commit()

        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        cp._consecutive_failures = 0
        cp._circuit_open_until = None
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 503
            mock_resp.text = "service unavailable"
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch.object(cp.settings, "conductor_url", "http://conductor:8000"):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    result = await cp.push_to_conductor()
                    assert result is False
                    assert cp._consecutive_failures == 1
        finally:
            session_mod._session_factory = prev_factory
            cp._consecutive_failures = 0
            cp._circuit_open_until = None

    async def test_push_circuit_breaker_trips_after_threshold(self, engine):
        """After _CIRCUIT_BREAKER_THRESHOLD failures, _circuit_open_until is set."""
        import orchestrator.core.conductor_push as cp
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import Team

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            db.add(Team(slug="cb-team", name="CB Team"))
            await db.commit()

        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        # Pre-load with N-1 failures
        cp._consecutive_failures = cp._CIRCUIT_BREAKER_THRESHOLD - 1
        cp._circuit_open_until = None
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "fail"
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch.object(cp.settings, "conductor_url", "http://conductor:8000"):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    result = await cp.push_to_conductor()
                    assert result is False
                    assert cp._circuit_open_until is not None
        finally:
            session_mod._session_factory = prev_factory
            cp._consecutive_failures = 0
            cp._circuit_open_until = None


# ════════════════════════════════════════════════════════════════════════════
# 2. MAINTENANCE — prune_batch_ids, compaction (recently fixed)
# ════════════════════════════════════════════════════════════════════════════


class TestMaintenanceDeep:
    async def test_prune_batch_ids_removes_old_records(self, engine):
        """End-to-end: prune_batch_ids deletes IngestBatch rows older than cutoff.

        This exercises the recently fixed code path that uses
        IngestBatch.batch_id (not the non-existent IngestBatch.id).
        """
        from orchestrator.core import maintenance as mod
        from orchestrator.db.models import App, IngestBatch, Team

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        async with factory() as db:
            db.add(Team(id=team_id, slug="prune-team", name="Prune Team"))
            await db.flush()
            db.add(App(
                id=app_uuid, team_id=team_id, app_id="prune-app",
                app_name="P", api_key_hash="x", api_key_prefix="mds_p",
            ))
            await db.flush()
            old = IngestBatch(
                batch_id="old-batch-1",
                app_id=app_uuid,
                record_count=10,
                received_at=datetime.now(timezone.utc) - timedelta(days=30),
            )
            new = IngestBatch(
                batch_id="new-batch-1",
                app_id=app_uuid,
                record_count=5,
                received_at=datetime.now(timezone.utc),
            )
            db.add_all([old, new])
            await db.commit()

        original = mod._session_factory
        try:
            mod._session_factory = factory
            await mod.prune_batch_ids()

            async with factory() as db:
                rows = (await db.execute(select(IngestBatch))).scalars().all()
                ids = [r.batch_id for r in rows]
                assert "new-batch-1" in ids
                assert "old-batch-1" not in ids
        finally:
            mod._session_factory = original

    async def test_compact_usage_records_no_old_data(self, engine):
        """compact_usage_records runs cleanly when there are no old records."""
        from orchestrator.core import maintenance as mod

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        original = mod._session_factory
        try:
            mod._session_factory = factory
            await mod.compact_usage_records()  # No data → returns cleanly
        finally:
            mod._session_factory = original

    async def test_compact_usage_records_no_factory(self):
        """compact_usage_records returns early when no session factory."""
        from orchestrator.core import maintenance as mod
        original = mod._session_factory
        try:
            mod._session_factory = None
            await mod.compact_usage_records()
        finally:
            mod._session_factory = original

    async def test_compact_hourly_to_daily_no_factory(self):
        from orchestrator.core import maintenance as mod
        original = mod._session_factory
        try:
            mod._session_factory = None
            await mod.compact_hourly_to_daily()
        finally:
            mod._session_factory = original

    async def test_compact_hourly_to_daily_no_data(self, engine):
        from orchestrator.core import maintenance as mod

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        original = mod._session_factory
        try:
            mod._session_factory = factory
            await mod.compact_hourly_to_daily()
        finally:
            mod._session_factory = original

    async def test_compact_usage_records_with_old_data(self, engine):
        """End-to-end: old UsageRecords get rolled up + deleted."""
        from orchestrator.core import maintenance as mod
        from orchestrator.db.models import App, Team, UsageRecord

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        async with factory() as db:
            db.add(Team(id=team_id, slug="compact-team", name="C"))
            await db.flush()
            db.add(App(
                id=app_uuid, team_id=team_id, app_id="compact-app",
                app_name="Compact", api_key_hash="x", api_key_prefix="mds_c",
            ))
            await db.flush()

            old_ts = datetime.now(timezone.utc) - timedelta(days=7)
            for i in range(3):
                db.add(UsageRecord(
                    app_id=app_uuid, team_id=team_id,
                    provider="openai", model="gpt-4o",
                    resource_type="llm_call",
                    timestamp=old_ts,
                    input_tokens=100, output_tokens=50,
                    total_tokens=150,
                    input_cost=Decimal("0.01"),
                    output_cost=Decimal("0.02"),
                    total_cost=Decimal("0.03"),
                    duration_ms=100 + i,
                ))
            await db.commit()

        original = mod._session_factory
        try:
            mod._session_factory = factory
            # Patch enqueue so we don't depend on the write_queue worker being up
            with patch(
                "orchestrator.core.write_queue.enqueue",
                new=AsyncMock(return_value=None),
            ):
                await mod.compact_usage_records()

            async with factory() as db:
                # Old records should now be deleted
                rows = (await db.execute(select(UsageRecord))).scalars().all()
                assert len(rows) == 0
        finally:
            mod._session_factory = original

    async def test_compact_hourly_to_daily_with_old_aggregates(self, engine):
        from orchestrator.core import maintenance as mod
        from orchestrator.db.models import App, Team, UsageAggregate

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        old_period = datetime.now(timezone.utc) - timedelta(days=14)
        old_period = old_period.replace(minute=0, second=0, microsecond=0)
        async with factory() as db:
            db.add(Team(id=team_id, slug="hd-team", name="HD"))
            await db.flush()
            db.add(App(
                id=app_uuid, team_id=team_id, app_id="hd-app",
                app_name="HD", api_key_hash="x", api_key_prefix="mds_h",
            ))
            await db.flush()
            db.add(UsageAggregate(
                app_id=app_uuid, team_id=team_id,
                provider="openai", model="gpt-4o",
                resource_type="llm_call",
                granularity="hourly",
                period_start=old_period,
                period_end=old_period + timedelta(hours=1),
                call_count=5, input_tokens=100, output_tokens=50,
                total_tokens=150,
                input_cost=Decimal("0.05"),
                output_cost=Decimal("0.10"),
                total_cost=Decimal("0.15"),
                duration_ms_sum=500,
                min_duration_ms=80, max_duration_ms=120,
            ))
            await db.commit()

        original = mod._session_factory
        try:
            mod._session_factory = factory
            with patch(
                "orchestrator.core.write_queue.enqueue",
                new=AsyncMock(return_value=None),
            ):
                await mod.compact_hourly_to_daily()

            async with factory() as db:
                hourly = (await db.execute(
                    select(UsageAggregate).where(UsageAggregate.granularity == "hourly")
                )).scalars().all()
                assert len(hourly) == 0
        finally:
            mod._session_factory = original

    async def test_create_incident_ticket_success(self):
        """create_incident_ticket returns True on a 200."""
        from orchestrator.core.maintenance import create_incident_ticket

        with patch("orchestrator.core.maintenance.urllib_request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            ok = await create_incident_ticket(
                "https://jira.example.com/api",
                {"severity": "critical", "metric": "cost",
                 "actual_value": "100", "threshold_value": "50",
                 "app_id": "a1", "app_name": "A", "team_id": "t1"},
            )
            assert ok is True

    async def test_create_incident_ticket_failure(self):
        from orchestrator.core.maintenance import create_incident_ticket

        with patch("orchestrator.core.maintenance.urllib_request.urlopen") as mock_open:
            mock_open.side_effect = Exception("HTTP 500")
            ok = await create_incident_ticket("https://x.com", {"severity": "critical"})
            assert ok is False


# ════════════════════════════════════════════════════════════════════════════
# 3. CONNECTION CHECKER — deeper paths
# ════════════════════════════════════════════════════════════════════════════


class TestConnectionCheckerDeep:
    async def test_test_connection_unknown_id_raises(self):
        from orchestrator.core.connection_checker import test_connection
        with pytest.raises(ValueError):
            await test_connection("does-not-exist")

    async def test_gather_all_connections_returns_list(self):
        from orchestrator.core.connection_checker import gather_all_connections
        conns = await gather_all_connections()
        assert isinstance(conns, list)
        ids = [c["id"] for c in conns]
        # Nomus is always included even if not configured
        assert "nomus" in ids

    async def test_test_all_connections_handles_exceptions(self):
        """test_all_connections catches per-connection exceptions and continues."""
        from orchestrator.core import connection_checker as cc

        async def boom(conn):
            raise RuntimeError("simulated test failure")

        with patch.object(cc, "_test_single_connection", new=boom):
            results = await cc.test_all_connections()
            assert isinstance(results, list)
            for r in results:
                assert "status" in r
                # Per-connection failures get marked as error
                assert r["status"] == "error"

    async def test_test_smtp_failure(self):
        """_test_smtp returns False when socket.create_connection raises."""
        import socket as sock_mod
        from orchestrator.core import connection_checker as cc

        with patch.object(sock_mod, "create_connection",
                          side_effect=OSError("simulated failure")):
            ok, err = await cc._test_smtp("any.host", port=587)
            assert ok is False
            assert err is not None

    def test_sanitize_connection(self):
        from orchestrator.core.connection_checker import sanitize_connection
        conn = {
            "id": "x",
            "_raw_url": "secret",
            "_raw_headers": {"k": "v"},
            "status": "connected",
        }
        result = sanitize_connection(conn)
        assert "_raw_url" not in result
        assert "_raw_headers" not in result
        assert result["status"] == "connected"

    async def test_test_single_connection_not_configured_skips(self):
        from orchestrator.core import connection_checker as cc
        cc.clear_cache()
        conn = {
            "id": "skip1",
            "category": "platform",
            "status": "not_configured",
            "type": "nomus",
            "_raw_url": "",
            "metadata": {},
        }
        result = await cc._test_single_connection(conn)
        assert result["status"] == "not_configured"

    async def test_test_single_connection_disabled_skips(self):
        from orchestrator.core import connection_checker as cc
        cc.clear_cache()
        conn = {
            "id": "skip2",
            "category": "platform",
            "status": "disabled",
            "type": "federation",
            "_raw_url": "https://example.com",
            "metadata": {},
        }
        result = await cc._test_single_connection(conn)
        assert result["status"] == "disabled"

    async def test_test_single_connection_smtp_path(self):
        from orchestrator.core import connection_checker as cc
        cc.clear_cache()
        conn = {
            "id": "smtp1",
            "category": "notification",
            "status": "unknown",
            "type": "smtp",
            "_raw_url": "smtp.test.invalid",
            "metadata": {"port": 587},
        }
        with patch.object(cc, "_test_smtp", new=AsyncMock(return_value=(True, None))):
            result = await cc._test_single_connection(conn)
            assert result["status"] == "connected"

    async def test_test_single_connection_smtp_fail(self):
        from orchestrator.core import connection_checker as cc
        cc.clear_cache()
        conn = {
            "id": "smtp2",
            "category": "notification",
            "status": "unknown",
            "type": "smtp",
            "_raw_url": "bad.host",
            "metadata": {"port": 587},
        }
        with patch.object(cc, "_test_smtp", new=AsyncMock(return_value=(False, "denied"))):
            result = await cc._test_single_connection(conn)
            assert result["status"] == "error"

    async def test_test_single_connection_http_error_status(self):
        from orchestrator.core import connection_checker as cc
        cc.clear_cache()
        conn = {
            "id": "err1",
            "category": "ai_provider",
            "status": "unknown",
            "type": "ai_provider",
            "_raw_url": "https://api.example.com",
            "_raw_headers": {},
            "metadata": {},
        }
        with patch.object(cc, "_async_check_url",
                          new=AsyncMock(return_value=(False, 0, "timeout"))):
            result = await cc._test_single_connection(conn)
            assert result["status"] == "error"
            assert result["last_error"] == "timeout"

    def test_gather_federation_disabled(self):
        from orchestrator.core.connection_checker import _gather_federation
        with patch("orchestrator.core.connection_checker.settings") as mock_s:
            mock_s.federation_enabled = False
            conns = _gather_federation()
            assert len(conns) == 1
            assert conns[0]["status"] == "disabled"

    def test_gather_federation_enabled_no_url(self):
        from orchestrator.core.connection_checker import _gather_federation
        with patch("orchestrator.core.connection_checker.settings") as mock_s:
            mock_s.federation_enabled = True
            mock_s.federation_aggregator_url = ""
            conns = _gather_federation()
            assert len(conns) == 1
            assert conns[0]["status"] == "not_configured"

    def test_gather_gateway_disabled(self):
        from orchestrator.core.connection_checker import _gather_gateway
        with patch("orchestrator.core.connection_checker.settings") as mock_s:
            mock_s.gateway_enabled = False
            conns = _gather_gateway()
            assert conns == []

    def test_gather_gateway_with_openai_upstream(self):
        from orchestrator.core.connection_checker import _gather_gateway
        with patch("orchestrator.core.connection_checker.settings") as mock_s:
            mock_s.gateway_enabled = True
            mock_s.gateway_openai_base_url = "https://upstream.openai.com"
            mock_s.gateway_anthropic_base_url = ""
            conns = _gather_gateway()
            assert len(conns) == 1
            assert conns[0]["id"] == "gateway_openai"

    def test_gather_gateway_with_both_upstreams(self):
        from orchestrator.core.connection_checker import _gather_gateway
        with patch("orchestrator.core.connection_checker.settings") as mock_s:
            mock_s.gateway_enabled = True
            mock_s.gateway_openai_base_url = "https://up.openai.com"
            mock_s.gateway_anthropic_base_url = "https://up.anthropic.com"
            conns = _gather_gateway()
            assert len(conns) == 2

    async def test_gather_notifications_full(self):
        # Reads the real DB notification config now, not phantom settings.
        from unittest.mock import AsyncMock
        from sqlalchemy.ext.asyncio import (
            AsyncSession, async_sessionmaker, create_async_engine,
        )
        from orchestrator.core import connection_checker as cc
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import Base

        eng = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        cfg = {
            "slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/x"},
            "teams": {"enabled": True, "webhook_url": "https://teams.example/x"},
            "email": {"enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 25,
                      "recipients": ["a@b.c"]},
            "pagerduty": {"enabled": True, "integration_key": "pd-key"},
            "webhook": {"enabled": True, "url": "https://hook.example/x"},
        }
        with patch.object(session_mod, "_session_factory", factory), \
                patch("orchestrator.api.notifications._load_config",
                      AsyncMock(return_value=cfg)):
            conns = await cc._gather_notifications()
        ids = [c["id"] for c in conns]
        assert "notify_slack" in ids
        assert "notify_teams" in ids
        assert "notify_smtp" in ids
        assert "notify_pagerduty" in ids
        assert "notify_custom" in ids
        await eng.dispose()

    def test_gather_nomus_not_configured(self):
        from orchestrator.core.connection_checker import _gather_nomus
        with patch("orchestrator.core.connection_checker.settings") as mock_s:
            mock_s.nomus_url = ""
            mock_s.nomus_auto_sync = False
            conns = _gather_nomus()
            # Returns a single not_configured entry
            assert len(conns) == 1
            assert conns[0]["status"] == "not_configured"


# ════════════════════════════════════════════════════════════════════════════
# 4. THRESHOLD EVALUATOR — _send_notifications + helpers
# ════════════════════════════════════════════════════════════════════════════


class TestThresholdNotificationDispatch:
    async def test_send_notifications_no_config(self):
        """No config_cache → early return."""
        from orchestrator.api import notifications as notif
        from orchestrator.core.threshold_evaluator import _send_notifications
        prev = notif._config_cache.copy()
        notif._config_cache.clear()
        try:
            await _send_notifications({"severity": "critical"})
        finally:
            notif._config_cache.update(prev)

    async def test_send_notifications_slack_below_min_severity(self):
        """Slack with min_severity=critical, alert is warning → not dispatched."""
        from orchestrator.api import notifications as notif
        from orchestrator.core.threshold_evaluator import _send_notifications

        prev = notif._config_cache.copy()
        notif._config_cache.clear()
        notif._config_cache.update({
            "slack": {
                "enabled": True,
                "webhook_url": "https://hooks.slack.com/x",
                "min_severity": "critical",
            }
        })
        try:
            with patch(
                "orchestrator.core.threshold_evaluator._notify_slack",
                new=AsyncMock(),
            ) as mock_slack:
                await _send_notifications({"severity": "warning"})
                mock_slack.assert_not_called()
        finally:
            notif._config_cache.clear()
            notif._config_cache.update(prev)

    async def test_send_notifications_dispatches_to_all(self):
        from orchestrator.api import notifications as notif
        from orchestrator.core.threshold_evaluator import _send_notifications

        prev = notif._config_cache.copy()
        prev_loaded = notif._cache_loaded
        notif._config_cache.clear()
        # Mark the cache as loaded so _send_notifications uses the primed
        # config instead of falling back to a DB load (restart-resilience path).
        notif._cache_loaded = True
        notif._config_cache.update({
            "slack": {"enabled": True, "webhook_url": "x", "min_severity": "warning"},
            "teams": {"enabled": True, "webhook_url": "x", "min_severity": "warning"},
            "email": {
                "enabled": True, "smtp_host": "s", "recipients": ["a@b.c"],
                "min_severity": "warning",
            },
            "pagerduty": {
                "enabled": True, "integration_key": "k",
                "min_severity": "warning",
            },
            "webhook": {"enabled": True, "url": "x", "min_severity": "warning"},
        })

        alert = {
            "alert_id": "a", "threshold_id": "t", "threshold_name": "T",
            "team_id": "tm", "severity": "critical",
            "metric": "total_cost", "actual_value": "100",
            "threshold_value": "50", "period": "daily",
            "tier_pct": 100, "tier_message": "msg",
        }

        try:
            with patch(
                "orchestrator.core.threshold_evaluator._notify_slack",
                new=AsyncMock(),
            ) as mock_s, patch(
                "orchestrator.core.threshold_evaluator._notify_teams",
                new=AsyncMock(),
            ) as mock_t, patch(
                "orchestrator.core.threshold_evaluator._notify_email",
                new=AsyncMock(),
            ) as mock_e, patch(
                "orchestrator.core.threshold_evaluator._notify_pagerduty",
                new=AsyncMock(),
            ) as mock_pd, patch(
                "orchestrator.core.threshold_evaluator._notify_webhook",
                new=AsyncMock(),
            ) as mock_w:
                await _send_notifications(alert)
                mock_s.assert_awaited_once()
                mock_t.assert_awaited_once()
                mock_e.assert_awaited_once()
                mock_pd.assert_awaited_once()
                mock_w.assert_awaited_once()
        finally:
            notif._config_cache.clear()
            notif._config_cache.update(prev)
            notif._cache_loaded = prev_loaded

    async def test_trigger_auto_pause_app_without_pause_url(self, engine):
        """_trigger_auto_pause is a no-op when app has no pause_endpoint_url."""
        from orchestrator.core import threshold_evaluator as te
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import App, Team

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        async with factory() as db:
            db.add(Team(id=team_id, slug="ap-team", name="AP"))
            await db.flush()
            db.add(App(
                id=app_uuid, team_id=team_id, app_id="ap-app",
                app_name="AP App", api_key_hash="x", api_key_prefix="mds_a",
                pause_endpoint_url=None,  # explicitly no URL
            ))
            await db.commit()

        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        # Also patch the imported symbol in threshold_evaluator
        prev_te_factory = te._session_factory
        te._session_factory = factory
        try:
            # Should not raise; just returns None
            await te._trigger_auto_pause({"app_id": app_uuid})
        finally:
            session_mod._session_factory = prev_factory
            te._session_factory = prev_te_factory

    async def test_trigger_auto_pause_no_app_id(self):
        from orchestrator.core import threshold_evaluator as te
        # No app_id → early return (no DB needed)
        await te._trigger_auto_pause({})

    async def test_trigger_incident_ticket_with_app_id(self, engine):
        from orchestrator.core import threshold_evaluator as te
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import App, Team

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        async with factory() as db:
            db.add(Team(id=team_id, slug="it-team", name="IT"))
            await db.flush()
            db.add(App(
                id=app_uuid, team_id=team_id, app_id="it-app",
                app_name="IT App", api_key_hash="x", api_key_prefix="mds_i",
            ))
            await db.commit()

        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        prev_te_factory = te._session_factory
        te._session_factory = factory
        try:
            with patch(
                "orchestrator.core.maintenance.create_incident_ticket",
                new=AsyncMock(return_value=True),
            ) as mock_ticket:
                await te._trigger_incident_ticket(
                    "https://jira.example/api",
                    {"app_id": app_uuid, "severity": "critical"},
                )
                mock_ticket.assert_awaited_once()
        finally:
            session_mod._session_factory = prev_factory
            te._session_factory = prev_te_factory

    async def test_evaluate_thresholds_with_no_thresholds(self, engine):
        """evaluate_thresholds runs through with no thresholds and returns 0."""
        from orchestrator.core import threshold_evaluator as te
        from orchestrator.db import session as session_mod

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        prev_factory = session_mod._session_factory
        session_mod._session_factory = factory
        prev_te_factory = te._session_factory
        te._session_factory = factory
        try:
            count = await te.evaluate_thresholds()
            assert count == 0
        finally:
            session_mod._session_factory = prev_factory
            te._session_factory = prev_te_factory


# ════════════════════════════════════════════════════════════════════════════
# 5. NOTIFICATIONS API — end-to-end via client fixture
# ════════════════════════════════════════════════════════════════════════════


class TestNotificationsApi:
    async def test_get_config_default(self, client):
        resp = await client.get("/api/v1/notifications/config")
        assert resp.status_code == 200
        body = resp.json()
        # Should return a default NotificationConfig structure
        assert "slack" in body
        assert body["slack"]["enabled"] is False

    async def test_save_and_load_config(self, client):
        config = {
            "slack": {
                "enabled": True,
                "webhook_url": "https://hooks.slack.com/services/xxx",
                "channel": "#alerts",
                "min_severity": "warning",
            },
            "teams": {"enabled": False, "webhook_url": None, "min_severity": "warning"},
            "email": {
                "enabled": False, "smtp_host": None, "smtp_port": 587,
                "smtp_use_tls": True, "smtp_username": None, "smtp_password": None,
                "from_address": None, "recipients": [],
                "min_severity": "warning",
            },
            "pagerduty": {
                "enabled": False, "integration_key": None,
                "service_name": None, "min_severity": "critical",
            },
            "webhook": {
                "enabled": False, "url": None,
                "secret_header": None, "secret_value": None,
                "min_severity": "warning",
            },
        }
        resp = await client.put("/api/v1/notifications/config", json=config)
        assert resp.status_code == 200
        body = resp.json()
        assert body["slack"]["enabled"] is True
        assert body["slack"]["channel"] == "#alerts"

    async def test_test_all_channels_when_none_enabled(self, client):
        # Reset cache to known-empty state
        from orchestrator.api import notifications as notif
        notif._config_cache.clear()
        notif._cache_loaded = True

        resp = await client.post("/api/v1/notifications/test")
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert any(r["channel"] == "none" for r in body["results"])

    async def test_test_one_channel_unknown(self, client):
        resp = await client.post("/api/v1/notifications/test/not_a_channel")
        assert resp.status_code == 422

    async def test_test_one_channel_slack_not_configured(self, client):
        from orchestrator.api import notifications as notif
        notif._config_cache.clear()
        notif._cache_loaded = True

        resp = await client.post("/api/v1/notifications/test/slack")
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"][0]["success"] is False

    async def test_test_one_channel_email_not_configured(self, client):
        from orchestrator.api import notifications as notif
        notif._config_cache.clear()
        notif._cache_loaded = True

        resp = await client.post("/api/v1/notifications/test/email")
        body = resp.json()
        assert body["results"][0]["success"] is False

    async def test_test_one_channel_pagerduty_not_configured(self, client):
        from orchestrator.api import notifications as notif
        notif._config_cache.clear()
        notif._cache_loaded = True

        resp = await client.post("/api/v1/notifications/test/pagerduty")
        body = resp.json()
        assert body["results"][0]["success"] is False

    async def test_test_one_channel_teams_not_configured(self, client):
        from orchestrator.api import notifications as notif
        notif._config_cache.clear()
        notif._cache_loaded = True

        resp = await client.post("/api/v1/notifications/test/teams")
        body = resp.json()
        assert body["results"][0]["success"] is False

    async def test_test_one_channel_webhook_not_configured(self, client):
        from orchestrator.api import notifications as notif
        notif._config_cache.clear()
        notif._cache_loaded = True

        resp = await client.post("/api/v1/notifications/test/webhook")
        body = resp.json()
        assert body["results"][0]["success"] is False

    def test_build_test_payload(self):
        from orchestrator.api.notifications import _build_test_payload
        payload = _build_test_payload()
        assert payload["type"] == "test"
        assert payload["severity"] == "warning"
        assert "fired_at" in payload


# ════════════════════════════════════════════════════════════════════════════
# 6. TOPOLOGY API — successful self-register + reconnection paths
# ════════════════════════════════════════════════════════════════════════════


class TestTopologySelfRegister:
    @pytest_asyncio.fixture
    async def team_with_token(self, db_session):
        from orchestrator.api.topology import _generate_team_token, _hash_team_token
        from orchestrator.db.models import Team

        team_id = str(uuid.uuid4())
        token = _generate_team_token()
        team = Team(
            id=team_id, slug="reg-team", name="Registration Team",
            registration_token_hash=_hash_team_token(token),
            registration_token_prefix=token[:20],
        )
        db_session.add(team)
        await db_session.flush()
        return {"team_id": team_id, "token": token}

    async def test_self_register_creates_app(self, client, team_with_token):
        body = {
            "app_id": "newapp123",
            "app_name": "New App",
            "environment": "production",
            "agent_version": "2.0.0",
        }
        resp = await client.post(
            "/api/v1/self-register", json=body,
            headers={"X-Modus-TeamToken": team_with_token["token"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["registered"] is True
        assert data["app_id"] == "newapp123"
        assert data["team_id"] == team_with_token["team_id"]
        assert data["api_key"]  # session token returned

    async def test_self_register_idempotent(self, client, team_with_token):
        """Calling self-register twice with the same app_id reuses the row."""
        body = {
            "app_id": "reusable",
            "app_name": "Reusable",
            "environment": "production",
        }
        first = await client.post(
            "/api/v1/self-register", json=body,
            headers={"X-Modus-TeamToken": team_with_token["token"]},
        )
        assert first.status_code == 200
        second = await client.post(
            "/api/v1/self-register", json=body,
            headers={"X-Modus-TeamToken": team_with_token["token"]},
        )
        assert second.status_code == 200
        # Same internal UUID across both calls
        assert first.json()["app_uuid"] == second.json()["app_uuid"]
        # Second call: registered=False
        assert second.json()["registered"] is False

    async def test_self_register_normalizes_app_id_slug(self, client, team_with_token):
        body = {
            "app_id": "Some Cool App With Spaces!!!",
            "app_name": "Cool App",
            "environment": "production",
        }
        resp = await client.post(
            "/api/v1/self-register", json=body,
            headers={"X-Modus-TeamToken": team_with_token["token"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Slug should be lowercase, hyphenated
        assert data["app_id"] == "some-cool-app-with-spaces"

    async def test_self_register_rejects_unknown_team_token(self, client):
        from orchestrator.api.topology import _generate_team_token
        unknown_token = _generate_team_token()
        body = {
            "app_id": "x", "app_name": "X",
            "environment": "production",
        }
        resp = await client.post(
            "/api/v1/self-register", json=body,
            headers={"X-Modus-TeamToken": unknown_token},
        )
        assert resp.status_code == 401

    async def test_resolve_team_token_wrong_prefix(self, db_session):
        from orchestrator.api.topology import _resolve_team_token
        result = await _resolve_team_token("not_mds_team_xxxx", db_session)
        assert result is None

    async def test_make_safe_app_id_edge_cases(self):
        from orchestrator.api.topology import _make_safe_app_id
        # Multiple consecutive dashes get collapsed
        assert _make_safe_app_id("foo---bar") == "foo-bar"
        # Surrounding dashes stripped
        assert _make_safe_app_id("---hello---") == "hello"
        # Numbers preserved
        assert _make_safe_app_id("app123") == "app123"


# ════════════════════════════════════════════════════════════════════════════
# 7. ROLES API — assignments and permissions catalog
# ════════════════════════════════════════════════════════════════════════════


class TestRolesAssignments:
    @pytest_asyncio.fixture
    async def role_user_team(self, db_session):
        from orchestrator.db.models import RbacRole, Team, User
        team = Team(slug="assign-team", name="Assign Team")
        db_session.add(team)
        await db_session.flush()

        role = RbacRole(
            name="assign_role",
            description="Test role",
            allow=["apps:read"],
        )
        db_session.add(role)
        await db_session.flush()

        user = User(
            email="testuser@example.com",
            display_name="Test User",
            is_active=True,
        )
        db_session.add(user)
        await db_session.flush()

        return {
            "team_id": str(team.id),
            "role_id": str(role.id),
            "user_id": str(user.id),
        }

    async def test_list_permissions(self, client):
        resp = await client.get("/api/v1/roles/permissions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        # Each entry has the expected shape
        first = data[0]
        assert "permission" in first
        assert "resource" in first
        assert "action" in first

    async def test_list_assignments_empty(self, client):
        resp = await client.get("/api/v1/roles/assignments")
        assert resp.status_code == 200

    async def test_assign_role(self, client, role_user_team):
        resp = await client.post(
            "/api/v1/roles/assignments",
            json={
                "user_id": role_user_team["user_id"],
                "role_id": role_user_team["role_id"],
                "team_id": role_user_team["team_id"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["user_id"] == role_user_team["user_id"]
        assert data["role_id"] == role_user_team["role_id"]
        assert data["team_id"] == role_user_team["team_id"]

    async def test_assign_role_unknown_user(self, client, role_user_team):
        resp = await client.post(
            "/api/v1/roles/assignments",
            json={
                "user_id": str(uuid.uuid4()),
                "role_id": role_user_team["role_id"],
                "team_id": role_user_team["team_id"],
            },
        )
        assert resp.status_code == 404

    async def test_assign_role_unknown_role(self, client, role_user_team):
        resp = await client.post(
            "/api/v1/roles/assignments",
            json={
                "user_id": role_user_team["user_id"],
                "role_id": str(uuid.uuid4()),
                "team_id": role_user_team["team_id"],
            },
        )
        assert resp.status_code == 404

    async def test_assign_role_unknown_team(self, client, role_user_team):
        resp = await client.post(
            "/api/v1/roles/assignments",
            json={
                "user_id": role_user_team["user_id"],
                "role_id": role_user_team["role_id"],
                "team_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 404

    async def test_assign_role_duplicate(self, client, role_user_team):
        body = {
            "user_id": role_user_team["user_id"],
            "role_id": role_user_team["role_id"],
            "team_id": role_user_team["team_id"],
        }
        first = await client.post("/api/v1/roles/assignments", json=body)
        assert first.status_code == 201
        second = await client.post("/api/v1/roles/assignments", json=body)
        assert second.status_code == 409

    async def test_revoke_assignment_not_found(self, client):
        resp = await client.delete(f"/api/v1/roles/assignments/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_clone_role(self, client, role_user_team):
        resp = await client.post(
            f"/api/v1/roles/{role_user_team['role_id']}/clone",
            json={"name": "clone_of_assign_role", "description": "cloned"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "clone_of_assign_role"
        assert data["is_system"] is False

    async def test_clone_role_source_not_found(self, client):
        resp = await client.post(
            f"/api/v1/roles/{uuid.uuid4()}/clone",
            json={"name": "ghost_clone"},
        )
        assert resp.status_code == 404

    async def test_clone_role_duplicate_name(self, client, role_user_team, db_session):
        from orchestrator.db.models import RbacRole
        existing = RbacRole(name="taken_clone", allow=[])
        db_session.add(existing)
        await db_session.flush()
        resp = await client.post(
            f"/api/v1/roles/{role_user_team['role_id']}/clone",
            json={"name": "taken_clone"},
        )
        assert resp.status_code == 409

    async def test_create_role_unknown_permission(self, client):
        resp = await client.post(
            "/api/v1/roles",
            json={
                "name": "bad_perms_role",
                "description": "Role with unknown perm",
                "allow": ["nonexistent:permission"],
            },
        )
        assert resp.status_code == 400

    async def test_create_role_wildcard_resource(self, client):
        # Try a valid wildcard like "apps:*"
        resp = await client.post(
            "/api/v1/roles",
            json={
                "name": "wildcard_role",
                "description": "Wildcard",
                "allow": ["apps:*"],
            },
        )
        # Either 201 (accepted) or 400 (unknown resource) is fine
        assert resp.status_code in (201, 400)


# ════════════════════════════════════════════════════════════════════════════
# 8. GATEWAY — _get_stream_budget_limit (recently fixed bug)
# ════════════════════════════════════════════════════════════════════════════


class TestGatewayStreamBudget:
    async def test_no_threshold_returns_zero(self, db_session):
        """Without an active app-level cost threshold, returns 0.0."""
        from orchestrator.api.gateway import _get_stream_budget_limit
        from orchestrator.db.models import App, Team

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        team = Team(id=team_id, slug="sb-team", name="SB Team")
        app = App(
            id=app_uuid, team_id=team_id, app_id="sb-app",
            app_name="SB", api_key_hash="x", api_key_prefix="mds_s",
        )
        db_session.add_all([team, app])
        await db_session.flush()

        limit = await _get_stream_budget_limit(app, db_session)
        assert limit == 0.0

    async def test_threshold_returns_critical_value(self, db_session):
        """With an active cost threshold, returns critical_value."""
        from orchestrator.api.gateway import _get_stream_budget_limit
        from orchestrator.db.models import App, Team, Threshold

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        team = Team(id=team_id, slug="sb-team-2", name="SB2")
        app = App(
            id=app_uuid, team_id=team_id, app_id="sb-app-2",
            app_name="SB2", api_key_hash="x", api_key_prefix="mds_s",
        )
        threshold = Threshold(
            team_id=team_id, app_id=app_uuid,
            name="cost-cap", scope="app", metric="cost",
            period="hourly",
            critical_value=Decimal("25.50"),
            is_active=True,
        )
        db_session.add_all([team, app, threshold])
        await db_session.flush()

        limit = await _get_stream_budget_limit(app, db_session)
        assert limit == 25.5

    async def test_inactive_threshold_ignored(self, db_session):
        """An inactive threshold should NOT contribute to the limit."""
        from orchestrator.api.gateway import _get_stream_budget_limit
        from orchestrator.db.models import App, Team, Threshold

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        team = Team(id=team_id, slug="sb-team-3", name="SB3")
        app = App(
            id=app_uuid, team_id=team_id, app_id="sb-app-3",
            app_name="SB3", api_key_hash="x", api_key_prefix="mds_s",
        )
        threshold = Threshold(
            team_id=team_id, app_id=app_uuid,
            name="cost-cap", scope="app", metric="cost",
            period="hourly",
            critical_value=Decimal("100.00"),
            is_active=False,  # disabled
        )
        db_session.add_all([team, app, threshold])
        await db_session.flush()

        limit = await _get_stream_budget_limit(app, db_session)
        assert limit == 0.0

    async def test_picks_lowest_critical_value(self, db_session):
        """When multiple active thresholds exist, picks the smallest critical_value."""
        from orchestrator.api.gateway import _get_stream_budget_limit
        from orchestrator.db.models import App, Team, Threshold

        team_id = str(uuid.uuid4())
        app_uuid = str(uuid.uuid4())
        team = Team(id=team_id, slug="sb-team-4", name="SB4")
        app = App(
            id=app_uuid, team_id=team_id, app_id="sb-app-4",
            app_name="SB4", api_key_hash="x", api_key_prefix="mds_s",
        )
        db_session.add_all([team, app])
        await db_session.flush()
        db_session.add_all([
            Threshold(
                team_id=team_id, app_id=app_uuid,
                name="cap-high", scope="app", metric="cost", period="hourly",
                critical_value=Decimal("100"),
                is_active=True,
            ),
            Threshold(
                team_id=team_id, app_id=app_uuid,
                name="cap-low", scope="app", metric="cost", period="hourly",
                critical_value=Decimal("10"),
                is_active=True,
            ),
        ])
        await db_session.flush()

        limit = await _get_stream_budget_limit(app, db_session)
        assert limit == 10.0


# ════════════════════════════════════════════════════════════════════════════
# 9. GOVERNANCE LOOP — broader detection rule paths with seeded data
# ════════════════════════════════════════════════════════════════════════════


class TestGovernanceLoopDeep:
    async def test_detect_pqc_migration_disabled(self, db_session):
        """When pqc_assessment_enabled is False, returns []."""
        from orchestrator.core.governance_loop import _detect_pqc_migration_needs
        # settings is imported lazily inside _detect_pqc_migration_needs from
        # orchestrator.core.config. Patch the module-level singleton there.
        with patch("orchestrator.core.config.settings") as mock_s:
            mock_s.pqc_assessment_enabled = False
            proposals = await _detect_pqc_migration_needs(db_session, str(uuid.uuid4()))
            assert proposals == []

    async def test_detect_rewind_patterns_no_data(self, db_session):
        from orchestrator.core.governance_loop import _detect_rewind_patterns
        proposals = await _detect_rewind_patterns(
            db_session, str(uuid.uuid4()), 24, 3,
        )
        assert proposals == []

    async def test_detect_sentinel_threats_no_data(self, db_session):
        from orchestrator.core.governance_loop import _detect_sentinel_threats
        proposals = await _detect_sentinel_threats(db_session, str(uuid.uuid4()))
        assert proposals == []

    async def test_run_governance_loop_full_path_no_teams(self, engine):
        """End-to-end: run_governance_loop with no teams completes cleanly."""
        from orchestrator.core import governance_loop as gl

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = gl._session_factory
        gl._session_factory = factory
        try:
            await gl.run_governance_loop()
        finally:
            gl._session_factory = prev

    async def test_run_governance_loop_with_team_no_data(self, engine):
        """Team exists but no usage data → loop runs detection but emits no proposals."""
        from orchestrator.core import governance_loop as gl
        from orchestrator.db.models import Team

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            db.add(Team(slug="g-team", name="G"))
            await db.commit()

        prev = gl._session_factory
        gl._session_factory = factory
        try:
            await gl.run_governance_loop()
        finally:
            gl._session_factory = prev

    def test_generate_pqc_migration_yaml_basic(self):
        from orchestrator.core.governance_loop import _generate_pqc_migration_yaml
        yaml = _generate_pqc_migration_yaml("hybrid", "RSA-2048")
        assert "type: pqc_migration" in yaml
        assert 'stage: "hybrid"' in yaml
        assert "RSA-2048" in yaml
        assert "enforcement: block" in yaml  # urgent path

    def test_generate_pqc_migration_yaml_unknown_algo(self):
        from orchestrator.core.governance_loop import _generate_pqc_migration_yaml
        yaml = _generate_pqc_migration_yaml("hybrid", "ECDSA-256")
        # Non-urgent algorithm gets warn + 180 days
        assert "enforcement: warn" in yaml
        assert "deadline_days: 180" in yaml

    def test_generate_pqc_migration_yaml_empty_inputs(self):
        from orchestrator.core.governance_loop import _generate_pqc_migration_yaml
        yaml = _generate_pqc_migration_yaml("", "")
        # Falls back to defaults
        assert 'stage: "assessment"' in yaml
        assert "unknown" in yaml
