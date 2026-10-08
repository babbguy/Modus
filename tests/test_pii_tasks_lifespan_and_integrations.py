"""
Targeted tests across several modules.

Targets:
  - sdk/modus/pii.py                       — pure-function unit tests
  - orchestrator/core/tasks.py                — cancellation paths for loops
  - orchestrator/core/pricing_sync.py         — fetch + sync paths
  - orchestrator/core/invitation_service.py   — provider send() paths
  - orchestrator/core/lifespan.py             — startup/shutdown integration
  - orchestrator/core/report_scheduler.py     — delivery + scheduler paths
  - orchestrator/core/insights_engine.py      — task loop + helpers
  - sdk/modus/otel_exporter.py             — broader exception paths
"""
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ─────────────────────────────────────────────────────────────────────────────
# 1. PII module — sdk/modus/pii.py
# ─────────────────────────────────────────────────────────────────────────────


class TestPiiScrub:
    def test_scrub_email(self):
        from modus.pii import scrub_pii
        result = scrub_pii("contact us at alice@example.com today")
        assert "<EMAIL>" in result
        assert "alice@example.com" not in result

    def test_scrub_phone(self):
        from modus.pii import scrub_pii
        result = scrub_pii("Call me at 555-123-4567")
        assert "<PHONE>" in result

    def test_scrub_ssn(self):
        from modus.pii import scrub_pii
        result = scrub_pii("SSN 123-45-6789")
        assert "<SSN>" in result

    def test_scrub_credit_card(self):
        from modus.pii import scrub_pii
        result = scrub_pii("card 4111-1111-1111-1111")
        assert "<CREDIT_CARD>" in result

    def test_scrub_api_key_authorization_header(self):
        # api_key regex requires "Bearer<space>" or "sk-/pk-/mds_" prefix followed by
        # 20+ alphanumeric chars with no other matching pattern interleaved.
        from modus.pii import scrub_pii
        result = scrub_pii("Authorization: Bearer " + "a" * 40)
        assert "<API_KEY>" in result

    def test_scrub_aws_key(self):
        from modus.pii import scrub_pii
        result = scrub_pii("key=AKIAIOSFODNN7EXAMPLE")
        assert "<AWS_KEY>" in result

    def test_scrub_connection_string(self):
        from modus.pii import scrub_pii
        result = scrub_pii("DSN postgres://user:pass@localhost/db")
        assert "<CONNECTION_STRING>" in result

    def test_scrub_non_string_returns_as_is(self):
        from modus.pii import scrub_pii
        assert scrub_pii(42) == 42  # type: ignore[arg-type]
        assert scrub_pii(None) is None  # type: ignore[arg-type]


class TestPiiScan:
    def test_scan_email_returns_finding(self):
        from modus.pii import scan_pii
        findings = scan_pii("alice@example.com and bob@example.com")
        types = [f["type"] for f in findings]
        assert "email" in types
        email = next(f for f in findings if f["type"] == "email")
        assert email["count"] == 2

    def test_scan_no_findings_returns_empty_list(self):
        from modus.pii import scan_pii
        assert scan_pii("just some plain text") == []

    def test_scan_non_string(self):
        from modus.pii import scan_pii
        assert scan_pii(123) == []  # type: ignore[arg-type]

    def test_scan_does_not_include_match_text(self):
        from modus.pii import scan_pii
        findings = scan_pii("alice@example.com")
        for f in findings:
            assert "match" not in f
            assert "alice" not in str(f)


class TestPiiScanAndRedact:
    def test_returns_redacted_and_findings(self):
        from modus.pii import scan_and_redact_pii
        text, findings = scan_and_redact_pii("ssn 123-45-6789 email a@b.co")
        assert "<SSN>" in text
        assert "<EMAIL>" in text
        types = {f["type"] for f in findings}
        assert "ssn" in types
        assert "email" in types

    def test_no_pii(self):
        from modus.pii import scan_and_redact_pii
        text, findings = scan_and_redact_pii("hello world")
        assert text == "hello world"
        assert findings == []

    def test_non_string(self):
        from modus.pii import scan_and_redact_pii
        text, findings = scan_and_redact_pii(42)  # type: ignore[arg-type]
        assert text == 42
        assert findings == []


class TestPiiScrubRecursive:
    def test_str_passthrough_to_scrub(self):
        from modus.pii import scrub_pii_recursive
        assert "<EMAIL>" in scrub_pii_recursive("foo@bar.com")

    def test_list(self):
        from modus.pii import scrub_pii_recursive
        result = scrub_pii_recursive(["alice@x.com", "plain"])
        assert "<EMAIL>" in result[0]
        assert result[1] == "plain"

    def test_tuple(self):
        from modus.pii import scrub_pii_recursive
        result = scrub_pii_recursive(("alice@x.com", "plain"))
        assert isinstance(result, tuple)
        assert "<EMAIL>" in result[0]

    def test_dict(self):
        from modus.pii import scrub_pii_recursive
        result = scrub_pii_recursive({"email": "alice@example.com", "ok": "no_pii"})
        assert "<EMAIL>" in result["email"]
        assert result["ok"] == "no_pii"

    def test_nested(self):
        from modus.pii import scrub_pii_recursive
        data = {"users": [{"contact": "x@y.com"}, "plain"]}
        result = scrub_pii_recursive(data)
        assert "<EMAIL>" in result["users"][0]["contact"]

    def test_other_types_pass_through(self):
        from modus.pii import scrub_pii_recursive
        assert scrub_pii_recursive(42) == 42
        assert scrub_pii_recursive(None) is None
        assert scrub_pii_recursive(True) is True


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tasks — cancellation paths for additional loops
# ─────────────────────────────────────────────────────────────────────────────


class TestTaskCancellation:
    """Each loop must exit cleanly when cancelled."""

    async def _run_and_cancel(self, coro_fn):
        task = asyncio.create_task(coro_fn())
        await asyncio.sleep(0.01)
        task.cancel()
        # Should not raise
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass

    async def test_pricing_sync_loop_cancellable(self):
        from orchestrator.core import tasks
        with patch(
            "orchestrator.core.pricing_sync.sync_pricing", new=AsyncMock(return_value=None)
        ):
            await self._run_and_cancel(tasks._run_pricing_sync_loop)

    async def test_anomaly_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_anomaly_loop)

    async def test_forecast_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_forecast_loop)

    async def test_recommendations_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_recommendations_loop)

    async def test_efficiency_audit_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_efficiency_audit_loop)

    async def test_governance_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_governance_loop)

    async def test_trajectory_fingerprint_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_trajectory_fingerprint_loop)

    async def test_trism_pattern_sync_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_trism_pattern_sync_loop)

    async def test_evolution_loop_task_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_evolution_loop_task)

    async def test_neuromorphic_metrics_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_neuromorphic_metrics_loop)


    async def test_neuro_assurance_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_neuro_assurance_loop)

    async def test_federation_sync_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_federation_sync_loop)

    async def test_pqc_assessment_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_pqc_assessment_loop)

    async def test_report_scheduler_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_report_scheduler_loop)

    async def test_routing_calibrator_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_routing_calibrator_loop)

    async def test_drift_monitor_loop_cancellable(self):
        from orchestrator.core import tasks
        await self._run_and_cancel(tasks._run_drift_monitor_loop)

class TestTaskExceptionRecovery:
    """Loops should swallow exceptions and continue (then exit on cancel)."""

    async def test_pricing_sync_initial_exception_logged(self):
        """Initial sync_pricing failure does not kill the loop."""
        from orchestrator.core import tasks

        call_count = {"n": 0}

        async def boom():
            call_count["n"] += 1
            raise RuntimeError("simulated failure")

        with patch("orchestrator.core.pricing_sync.sync_pricing", new=boom):
            task = asyncio.create_task(tasks._run_pricing_sync_loop())
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.CancelledError:
                pass
        # Initial run was attempted
        assert call_count["n"] >= 1

    async def test_attribution_loop_recovers_from_exception(self):
        from orchestrator.core import tasks

        async def boom():
            raise RuntimeError("simulated")

        # Patch the module-level `process_pending_sessions` import inside the loop
        with patch(
            "orchestrator.core.attribution_engine.process_pending_sessions", new=boom
        ):
            task = asyncio.create_task(tasks._run_attribution_loop())
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.CancelledError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# 3. pricing_sync — full sync path against in-memory DB
# ─────────────────────────────────────────────────────────────────────────────


class TestPricingSync:
    async def test_sync_pricing_populates_table(self, engine):
        """End-to-end: sync_pricing inserts BUNDLED_PRICING rows into the DB."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

        from orchestrator.core import pricing_sync as ps
        from orchestrator.db import session as session_mod
        from orchestrator.db.models import PricingModel

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        prev = session_mod._session_factory
        session_mod._session_factory = factory
        # Also patch the ref in pricing_sync (it imports _session_factory at module load)
        ps._session_factory = factory
        try:
            await ps.sync_pricing()
        finally:
            session_mod._session_factory = prev
            ps._session_factory = prev

        # Verify we inserted at least one pricing row
        async with factory() as db:
            rows = (await db.execute(select(PricingModel))).scalars().all()
            assert len(rows) > 0
            providers = {r.provider for r in rows}
            assert "openai" in providers or "anthropic" in providers

    async def test_fetch_live_pricing_swallows_errors(self):
        """_fetch_live_pricing should never raise."""
        from orchestrator.core.pricing_sync import _fetch_live_pricing

        with patch(
            "orchestrator.core.pricing_sync._probe_openai_models",
            side_effect=RuntimeError("boom"),
        ):
            result = await _fetch_live_pricing()
            assert result == []

    def test_probe_openai_models_returns_unknown_models(self):
        """Models not in BUNDLED_PRICING are returned (drift detection)."""
        from orchestrator.core.pricing_sync import _probe_openai_models
        import json

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({
            "data": [
                {"id": "gpt-4o"},
                {"id": "gpt-99-future-model"},  # unknown
            ]
        }).encode()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda self, *a: None

        with patch("urllib.request.urlopen", return_value=fake_response):
            result = _probe_openai_models()
            assert "gpt-99-future-model" in result
            assert "gpt-4o" not in result  # already bundled


# ─────────────────────────────────────────────────────────────────────────────
# 4. Invitation Service — provider send() paths
# ─────────────────────────────────────────────────────────────────────────────


class TestEmailProviderSend:
    async def test_send_returns_false_when_smtp_fails(self):
        from orchestrator.core.invitation_service import EmailProvider, InvitationMessage

        os.environ["MODUS_INVITE_EMAIL_SMTP_HOST"] = "smtp.test.invalid"
        try:
            with patch("aiosmtplib.send", new=AsyncMock(side_effect=Exception("connection refused"))):
                p = EmailProvider()
                msg = InvitationMessage(
                    recipient_email="alice@example.com",
                    recipient_name="Alice",
                    inviter_name="Bob",
                    role_name="Admin",
                    team_name="Eng",
                    invite_url="https://example.com/invite",
                )
                result = await p.send(msg)
                assert result is False
        finally:
            del os.environ["MODUS_INVITE_EMAIL_SMTP_HOST"]

    async def test_send_returns_true_on_success(self):
        from orchestrator.core.invitation_service import EmailProvider, InvitationMessage

        os.environ["MODUS_INVITE_EMAIL_SMTP_HOST"] = "smtp.test.invalid"
        try:
            with patch("aiosmtplib.send", new=AsyncMock(return_value=None)):
                p = EmailProvider()
                msg = InvitationMessage(
                    recipient_email="alice@example.com",
                    recipient_name="Alice",
                    inviter_name="Bob",
                    role_name="Admin",
                    team_name=None,  # exercise the no-team path
                    invite_url="https://example.com/invite",
                )
                result = await p.send(msg)
                assert result is True
        finally:
            del os.environ["MODUS_INVITE_EMAIL_SMTP_HOST"]


class TestSlackProviderSend:
    async def test_send_returns_true_on_2xx(self):
        from orchestrator.core.invitation_service import SlackProvider, InvitationMessage

        os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/x"
        try:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock(return_value=None)
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("httpx.AsyncClient", return_value=mock_client):
                p = SlackProvider()
                msg = InvitationMessage(
                    recipient_email="x@x.com",
                    recipient_name="X",
                    inviter_name="Y",
                    role_name="Member",
                    team_name="Team A",
                    invite_url="https://example.com/i",
                )
                assert await p.send(msg) is True
        finally:
            del os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"]

    async def test_send_returns_false_on_error(self):
        from orchestrator.core.invitation_service import SlackProvider, InvitationMessage

        os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/x"
        try:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("HTTP error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("httpx.AsyncClient", return_value=mock_client):
                p = SlackProvider()
                msg = InvitationMessage(
                    recipient_email="x@x.com",
                    recipient_name="X",
                    inviter_name="Y",
                    role_name="Member",
                    team_name=None,
                    invite_url="https://example.com/i",
                )
                assert await p.send(msg) is False
        finally:
            del os.environ["MODUS_INVITE_SLACK_WEBHOOK_URL"]


class TestTeamsProviderSend:
    async def test_send_returns_true_on_success(self):
        from orchestrator.core.invitation_service import TeamsProvider, InvitationMessage

        os.environ["MODUS_INVITE_TEAMS_WEBHOOK_URL"] = "https://teams.example.com/x"
        try:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock(return_value=None)
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("httpx.AsyncClient", return_value=mock_client):
                p = TeamsProvider()
                msg = InvitationMessage(
                    recipient_email="x@x.com",
                    recipient_name="X",
                    inviter_name="Y",
                    role_name="Member",
                    team_name="Eng",
                    invite_url="https://example.com/i",
                )
                assert await p.send(msg) is True
        finally:
            del os.environ["MODUS_INVITE_TEAMS_WEBHOOK_URL"]

    async def test_send_returns_false_on_failure(self):
        from orchestrator.core.invitation_service import TeamsProvider, InvitationMessage

        os.environ["MODUS_INVITE_TEAMS_WEBHOOK_URL"] = "https://teams.example.com/x"
        try:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("network err"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("httpx.AsyncClient", return_value=mock_client):
                p = TeamsProvider()
                msg = InvitationMessage(
                    recipient_email="x@x.com",
                    recipient_name="X",
                    inviter_name="Y",
                    role_name="Member",
                    team_name=None,
                    invite_url="https://example.com/i",
                )
                assert await p.send(msg) is False
        finally:
            del os.environ["MODUS_INVITE_TEAMS_WEBHOOK_URL"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Lifespan — startup/shutdown context manager
# ─────────────────────────────────────────────────────────────────────────────


class TestLifespan:
    async def test_lifespan_starts_and_stops_cleanly(self):
        """Run the lifespan context manager; ensure background tasks start and shut down."""
        from fastapi import FastAPI
        from orchestrator.core.lifespan import lifespan

        # Patch heavy/blocking dependencies the lifespan calls during startup
        # so we exercise the orchestration logic without needing real DB/IO.
        with (
            patch("orchestrator.core.lifespan.init_db", new=AsyncMock(return_value=None)),
            patch("orchestrator.core.lifespan.close_db", new=AsyncMock(return_value=None)),
            patch(
                "orchestrator.core.write_queue.start_writer",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "orchestrator.core.write_queue.stop_writer",
                new=AsyncMock(return_value=None),
            ),
            # Avoid immediate eager DB or network calls in tasks that fire on startup
            patch(
                "orchestrator.core.pricing_sync.sync_pricing",
                new=AsyncMock(return_value=None),
            ),
        ):
            app = FastAPI()
            cm = lifespan(app)
            # Enter — runs startup
            await cm.__aenter__()
            # Exit — runs shutdown (cancels background tasks)
            await cm.__aexit__(None, None, None)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Report scheduler — async paths beyond unit helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestReportSchedulerAsync:
    async def test_deliver_webhook_returns_true_on_2xx(self):
        from orchestrator.core.report_scheduler import _deliver_webhook

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda self: self
        fake_resp.__exit__ = lambda self, *a: None

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = await _deliver_webhook(
                "https://example.com/hook", b'{"x":1}', "application/json"
            )
            assert result is True

    async def test_deliver_webhook_returns_false_on_5xx(self):
        from orchestrator.core.report_scheduler import _deliver_webhook

        fake_resp = MagicMock()
        fake_resp.status = 500
        fake_resp.__enter__ = lambda self: self
        fake_resp.__exit__ = lambda self, *a: None

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = await _deliver_webhook("https://example.com/hook", b"{}", "application/json")
            assert result is False

    async def test_deliver_webhook_returns_false_on_exception(self):
        from orchestrator.core.report_scheduler import _deliver_webhook

        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = await _deliver_webhook("https://example.com/hook", b"{}", "application/json")
            assert result is False

    async def test_run_report_scheduler_no_session_factory(self):
        """run_report_scheduler returns early when no DB factory is configured."""
        from orchestrator.core import report_scheduler as rs
        prev = rs._session_factory
        rs._session_factory = None
        try:
            await rs.run_report_scheduler()  # should not raise
        finally:
            rs._session_factory = prev

    async def test_generate_and_deliver_no_session_factory(self):
        from orchestrator.core import report_scheduler as rs
        prev = rs._session_factory
        rs._session_factory = None
        try:
            class FakeReport:
                report_type = "chargeback"
                delivery_channel = "webhook"
                delivery_target = "https://example.com"
                format = "json"
                name = "Fake"
                cost_center_id = None
                last_run_at = None
            ok = await rs.generate_and_deliver_report(FakeReport())
            assert ok is False
        finally:
            rs._session_factory = prev

    async def test_generate_and_deliver_unknown_type(self, engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import report_scheduler as rs

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = rs._session_factory
        rs._session_factory = factory
        try:
            class FakeReport:
                report_type = "nonexistent_type"
                delivery_channel = "webhook"
                delivery_target = "https://example.com"
                format = "json"
                name = "Fake"
                cost_center_id = None
                last_run_at = None
            ok = await rs.generate_and_deliver_report(FakeReport())
            assert ok is False
        finally:
            rs._session_factory = prev

    async def test_run_report_scheduler_with_no_active_reports(self, engine):
        """run_report_scheduler runs through the loop with no FinanceReports configured."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import report_scheduler as rs

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = rs._session_factory
        rs._session_factory = factory
        try:
            await rs.run_report_scheduler()  # No reports → no-op
        finally:
            rs._session_factory = prev

    async def test_generate_and_deliver_audit_trail_csv(self, engine):
        """Exercises generate_and_deliver path with audit_trail report + CSV format."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import report_scheduler as rs
        from orchestrator.db.models import FinanceReport

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = rs._session_factory
        rs._session_factory = factory

        # Pre-create the FinanceReport row in the shared engine
        async with factory() as db:
            report = FinanceReport(
                name="My Audit Report",
                report_type="audit_trail",
                schedule="daily",
                delivery_channel="webhook",
                delivery_target="https://example.com/webhook",
                format="csv",
                is_active=True,
            )
            db.add(report)
            await db.commit()
            report_id = report.id

        try:
            # Mock the webhook to succeed
            fake_resp = MagicMock()
            fake_resp.status = 200
            fake_resp.__enter__ = lambda self: self
            fake_resp.__exit__ = lambda self, *a: None
            with patch("urllib.request.urlopen", return_value=fake_resp):
                # Re-fetch the report and deliver
                async with factory() as db:
                    report = await db.get(FinanceReport, report_id)
                    ok = await rs.generate_and_deliver_report(report)
                    assert ok is True
        finally:
            rs._session_factory = prev

    async def test_generate_and_deliver_slack_format(self, engine):
        """Exercise the slack delivery path with a chargeback report."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import report_scheduler as rs
        from orchestrator.db.models import FinanceReport

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = rs._session_factory
        rs._session_factory = factory

        async with factory() as db:
            report = FinanceReport(
                name="Slack Chargeback",
                report_type="chargeback",
                schedule="weekly",
                delivery_channel="slack",
                delivery_target="https://hooks.slack.com/x",
                format="json",
                is_active=True,
            )
            db.add(report)
            await db.commit()
            report_id = report.id

        try:
            fake_resp = MagicMock()
            fake_resp.status = 200
            fake_resp.__enter__ = lambda self: self
            fake_resp.__exit__ = lambda self, *a: None
            with patch("urllib.request.urlopen", return_value=fake_resp):
                async with factory() as db:
                    report = await db.get(FinanceReport, report_id)
                    ok = await rs.generate_and_deliver_report(report)
                    assert ok is True
        finally:
            rs._session_factory = prev


# ─────────────────────────────────────────────────────────────────────────────
# 7. Insights engine — task loop wrapper and helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestInsightsHelpers:
    async def test_get_setting_returns_default(self, db_session):
        from orchestrator.core.insights_engine import get_setting
        # Built-in default for a known key
        v = await get_setting(db_session, "task.anomaly_scan.enabled")
        assert v == "true"

    async def test_get_setting_returns_empty_for_unknown(self, db_session):
        from orchestrator.core.insights_engine import get_setting
        v = await get_setting(db_session, "nonexistent.setting.key")
        assert v == ""

    async def test_get_setting_int(self, db_session):
        from orchestrator.core.insights_engine import get_setting_int
        v = await get_setting_int(db_session, "task.anomaly_scan.interval_seconds", 999)
        assert v == 300  # from defaults

    async def test_get_setting_int_invalid(self, db_session):
        """Invalid int falls back to provided default."""
        from orchestrator.core.insights_engine import get_setting_int
        # An unknown key returns "", which int() can't parse → falls back
        v = await get_setting_int(db_session, "nonexistent", 42)
        assert v == 42

    async def test_get_setting_float(self, db_session):
        from orchestrator.core.insights_engine import get_setting_float
        v = await get_setting_float(db_session, "insights.anomaly.zscore_threshold", 0.0)
        assert v == 3.0

    async def test_get_setting_float_invalid(self, db_session):
        from orchestrator.core.insights_engine import get_setting_float
        v = await get_setting_float(db_session, "nonexistent", 1.5)
        assert v == 1.5

    async def test_get_setting_bool_true(self, db_session):
        from orchestrator.core.insights_engine import get_setting_bool
        v = await get_setting_bool(db_session, "task.anomaly_scan.enabled", False)
        assert v is True

    async def test_get_setting_bool_false_for_unknown_with_default_false(self, db_session):
        from orchestrator.core.insights_engine import get_setting_bool
        # Unknown setting → empty string → not "true/1/yes/on"
        v = await get_setting_bool(db_session, "nonexistent", True)
        assert v is False  # Empty string is falsy

    def test_fallback_explain_anomaly(self):
        from orchestrator.core.insights_engine import _fallback_explain
        out = _fallback_explain("z-score is 4 — anomaly detected")
        assert "anomaly" in out.lower() or "spend" in out.lower()

    def test_fallback_explain_recommend(self):
        from orchestrator.core.insights_engine import _fallback_explain
        out = _fallback_explain("recommend switching to a cheaper model")
        assert "switch" in out.lower() or "cheaper" in out.lower()

    def test_fallback_explain_default(self):
        from orchestrator.core.insights_engine import _fallback_explain
        out = _fallback_explain("some context unrelated")
        assert "without AI" in out

    def test_ols_forecast_basic_linear(self):
        from orchestrator.core.insights_engine import _ols_forecast
        # y = 2x perfectly
        slope, intercept, r2 = _ols_forecast([0.0, 1.0, 2.0, 3.0], [0.0, 2.0, 4.0, 6.0])
        assert abs(slope - 2.0) < 1e-6
        assert abs(intercept) < 1e-6
        assert abs(r2 - 1.0) < 1e-6

    def test_ols_forecast_single_point(self):
        from orchestrator.core.insights_engine import _ols_forecast
        slope, intercept, r2 = _ols_forecast([1.0], [5.0])
        assert slope == 0.0
        assert intercept == 5.0
        assert r2 == 0.0

    def test_ols_forecast_empty(self):
        from orchestrator.core.insights_engine import _ols_forecast
        slope, intercept, r2 = _ols_forecast([], [])
        assert slope == 0.0
        assert intercept == 0.0
        assert r2 == 0.0

    def test_ols_forecast_zero_variance(self):
        from orchestrator.core.insights_engine import _ols_forecast
        slope, intercept, r2 = _ols_forecast([1.0, 1.0, 1.0], [3.0, 3.0, 3.0])
        # Zero variance in x → slope=0, intercept=mean(y)
        assert slope == 0.0
        assert intercept == 3.0

    def test_claude_generate_returns_none_on_error(self):
        from orchestrator.core.insights_engine import _claude_generate
        with patch("urllib.request.urlopen", side_effect=Exception("no network")):
            result = _claude_generate("system", "user", 100, "sk-test")
            assert result is None

    async def test_ai_explain_falls_back_when_api_unavailable(self):
        from orchestrator.core.insights_engine import _ai_explain

        # _claude_generate returns None on failure; _ai_explain falls back
        with patch(
            "orchestrator.core.insights_engine._claude_generate", return_value=None
        ):
            result = await _ai_explain(
                "you are an analyst", "anomaly detected for app X", 80
            )
            # Falls back to template-based explanation
            assert result is not None
            assert isinstance(result, str)
            assert len(result) > 0


class TestInsightsTaskLoop:
    async def test_loop_no_session_factory_continues(self):
        """When _session_factory is None, the loop continues but doesn't crash."""
        from orchestrator.core import insights_engine as ie

        prev = ie._session_factory
        ie._session_factory = None
        ran = {"n": 0}

        async def run_fn():
            ran["n"] += 1

        try:
            task = asyncio.create_task(
                ie.insights_task_loop("test.task", run_fn, 1)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.CancelledError:
                pass
        finally:
            ie._session_factory = prev

    async def test_loop_cancellable_with_session(self, engine):
        """Loop exits cleanly on cancel even with valid session factory."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import insights_engine as ie

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = ie._session_factory
        ie._session_factory = factory

        async def run_fn():
            pass  # no-op

        try:
            task = asyncio.create_task(
                ie.insights_task_loop("test.task2", run_fn, 1)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.CancelledError:
                pass
        finally:
            ie._session_factory = prev

    async def test_loop_swallows_run_fn_exception(self, engine):
        """If run_fn raises, the loop should keep going (until cancelled)."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import insights_engine as ie

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = ie._session_factory
        ie._session_factory = factory

        async def boom():
            raise RuntimeError("simulated")

        try:
            task = asyncio.create_task(
                ie.insights_task_loop("test.task3", boom, 1)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.CancelledError:
                pass
        finally:
            ie._session_factory = prev


class TestRunAnomalyScanEarlyReturn:
    """run_anomaly_scan / run_forecast_update return early on SQLite. Cover the early-return paths."""

    async def test_anomaly_scan_skips_on_sqlite(self, engine):
        """Hit the SQLite-skip branch (requires _session_factory set)."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import insights_engine as ie

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = ie._session_factory
        ie._session_factory = factory
        try:
            await ie.run_anomaly_scan()  # should return at SQLite check
        finally:
            ie._session_factory = prev

    async def test_forecast_update_skips_on_sqlite(self, engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from orchestrator.core import insights_engine as ie

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        prev = ie._session_factory
        ie._session_factory = factory
        try:
            await ie.run_forecast_update()
        finally:
            ie._session_factory = prev

    async def test_anomaly_scan_no_session_factory(self):
        from orchestrator.core import insights_engine as ie
        prev = ie._session_factory
        ie._session_factory = None
        try:
            await ie.run_anomaly_scan()
        finally:
            ie._session_factory = prev

    async def test_forecast_update_no_session_factory(self):
        from orchestrator.core import insights_engine as ie
        prev = ie._session_factory
        ie._session_factory = None
        try:
            await ie.run_forecast_update()
        finally:
            ie._session_factory = prev

    async def test_recommendation_refresh_no_session_factory(self):
        from orchestrator.core import insights_engine as ie
        prev = ie._session_factory
        ie._session_factory = None
        try:
            await ie.run_recommendation_refresh()
        finally:
            ie._session_factory = prev

    async def test_efficiency_audit_no_session_factory(self):
        from orchestrator.core import insights_engine as ie
        prev = ie._session_factory
        ie._session_factory = None
        try:
            await ie.run_prompt_efficiency_audit()
        finally:
            ie._session_factory = prev


# ─────────────────────────────────────────────────────────────────────────────
# 8. OTel exporter — additional paths
# ─────────────────────────────────────────────────────────────────────────────


class TestOtelExporter:
    def test_shutdown_with_no_providers(self):
        """_shutdown is a no-op when nothing has been initialized."""
        import modus.otel_exporter as otel
        prev_p, prev_mp = otel._provider, otel._meter_provider
        otel._provider = None
        otel._meter_provider = None
        try:
            otel._shutdown()  # should not raise
        finally:
            otel._provider = prev_p
            otel._meter_provider = prev_mp

    def test_shutdown_with_mock_providers(self):
        """_shutdown calls shutdown() on each provider when set."""
        import modus.otel_exporter as otel
        prev_p, prev_mp = otel._provider, otel._meter_provider

        mock_p = MagicMock()
        mock_mp = MagicMock()
        otel._provider = mock_p
        otel._meter_provider = mock_mp
        try:
            otel._shutdown()
            mock_p.shutdown.assert_called_once()
            mock_mp.shutdown.assert_called_once()
        finally:
            otel._provider = prev_p
            otel._meter_provider = prev_mp

    def test_record_call_executes_when_enabled(self):
        """record_call exercises span + counter logic when tracer/meter set."""
        import modus.otel_exporter as otel

        prev = (
            otel._tracer, otel._meter,
            otel._cost_counter, otel._token_counter,
            otel._call_counter, otel._duration_histogram, otel._error_counter,
        )

        # Build a minimal mock tracer that supports the context-manager span pattern
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=None)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        otel._tracer = mock_tracer
        otel._meter = MagicMock()
        otel._cost_counter = MagicMock()
        otel._token_counter = MagicMock()
        otel._call_counter = MagicMock()
        otel._duration_histogram = MagicMock()
        otel._error_counter = MagicMock()

        try:
            otel.record_call(
                provider="openai", model="gpt-4o",
                input_tokens=100, output_tokens=50,
                total_cost=0.01, duration_ms=200,
                app_id="a", team_id="t", environment="prod",
                extra_attributes={"k": "v"},
            )
            assert otel._call_counter.add.called
            assert otel._cost_counter.add.called
            assert otel._token_counter.add.called  # input AND output → at least one call
            assert otel._duration_histogram.record.called
        finally:
            (
                otel._tracer, otel._meter,
                otel._cost_counter, otel._token_counter,
                otel._call_counter, otel._duration_histogram, otel._error_counter,
            ) = prev

    def test_record_call_with_error(self):
        """record_call with an error attribute exercises the error counter path."""
        import modus.otel_exporter as otel

        prev = (
            otel._tracer, otel._meter,
            otel._cost_counter, otel._token_counter,
            otel._call_counter, otel._duration_histogram, otel._error_counter,
        )

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=None)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        otel._tracer = mock_tracer
        otel._meter = MagicMock()
        otel._cost_counter = MagicMock()
        otel._token_counter = MagicMock()
        otel._call_counter = MagicMock()
        otel._duration_histogram = MagicMock()
        otel._error_counter = MagicMock()

        # Patch trace_status_error so we don't need real OTel
        with patch(
            "modus.otel_exporter.trace_status_error",
            return_value=MagicMock(),
        ):
            try:
                otel.record_call(
                    provider="openai", model="gpt-4o",
                    input_tokens=0, output_tokens=0,
                    total_cost=0.0, duration_ms=0,
                    status="error", error="rate_limited",
                )
                assert otel._error_counter.add.called
            finally:
                (
                    otel._tracer, otel._meter,
                    otel._cost_counter, otel._token_counter,
                    otel._call_counter, otel._duration_histogram, otel._error_counter,
                ) = prev

    def test_record_policy_decision_when_enabled(self):
        import modus.otel_exporter as otel
        prev_t = otel._tracer

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=None)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        otel._tracer = mock_tracer
        try:
            otel.record_policy_decision(
                decision="deny",
                policy_name="budget_cap",
                policy_type="budget_cap",
                reason="over budget",
                provider="openai",
                model="gpt-4o",
                app_id="a",
            )
            mock_tracer.start_as_current_span.assert_called_once()
        finally:
            otel._tracer = prev_t


# ─────────────────────────────────────────────────────────────────────────────
# 9. Users API — additional paths
# ─────────────────────────────────────────────────────────────────────────────


class TestUsersApiAdditional:
    """Hits invitation creation, listing, revoke, accept paths via the API."""

    @pytest_asyncio.fixture
    async def role_team(self, db_session):
        from orchestrator.db.models import RbacRole, Team
        team_id = str(uuid.uuid4())
        role_id = str(uuid.uuid4())
        team = Team(id=team_id, slug="invite-team", name="Invite Team")
        role = RbacRole(
            id=role_id,
            name="invite_role",
            description="Test role",
            allow=["users:read", "users:write", "users:delete", "users:invite",
                   "view:overview"],
        )
        db_session.add(team)
        db_session.add(role)
        await db_session.flush()
        return {"team_id": team_id, "role_id": role_id}

    async def test_create_invitation(self, client, role_team):
        body = {
            "email": "newperson@example.com",
            "display_name": "New Person",
            "role_id": role_team["role_id"],
            "team_id": role_team["team_id"],
            "channel": "email",
        }
        resp = await client.post("/api/v1/users/invite", json=body)
        # Channel may not be configured in test env → 201 either way
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "newperson@example.com"
        assert data["status"] == "pending"

    async def test_create_invitation_unknown_role(self, client, role_team):
        body = {
            "email": "a@b.c",
            "display_name": "A",
            "role_id": str(uuid.uuid4()),
            "team_id": role_team["team_id"],
            "channel": "email",
        }
        resp = await client.post("/api/v1/users/invite", json=body)
        assert resp.status_code == 404

    async def test_create_invitation_unknown_team(self, client, role_team):
        body = {
            "email": "b@c.d",
            "display_name": "B",
            "role_id": role_team["role_id"],
            "team_id": str(uuid.uuid4()),
            "channel": "email",
        }
        resp = await client.post("/api/v1/users/invite", json=body)
        assert resp.status_code == 404

    async def test_create_invitation_user_already_exists(self, client, role_team, db_session):
        from orchestrator.db.models import User
        existing = User(
            email="exists@example.com",
            display_name="Existing",
            is_active=True,
        )
        db_session.add(existing)
        await db_session.flush()

        body = {
            "email": "exists@example.com",
            "display_name": "Existing",
            "role_id": role_team["role_id"],
            "team_id": role_team["team_id"],
            "channel": "email",
        }
        resp = await client.post("/api/v1/users/invite", json=body)
        assert resp.status_code == 409


    async def test_revoke_invitation_not_found(self, client):
        resp = await client.post(f"/api/v1/users/invitations/{uuid.uuid4()}/revoke")
        assert resp.status_code == 404

    async def test_accept_invitation_invalid_token_format(self, client):
        resp = await client.post(
            "/api/v1/users/invitations/accept",
            json={"token": "not_a_real_invite_token"},
        )
        assert resp.status_code == 400

    async def test_accept_invitation_unknown_token(self, client):
        # Properly prefixed but not in DB
        resp = await client.post(
            "/api/v1/users/invitations/accept",
            json={"token": "mds_invite_" + "x" * 40},
        )
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 10. Topology API — additional paths
# ─────────────────────────────────────────────────────────────────────────────


class TestTopologyAdditional:
    async def test_self_register_no_token_unauthorized(self, client):
        body = {
            "app_id": "my-app",
            "app_name": "My App",
            "environment": "production",
        }
        resp = await client.post("/api/v1/self-register", json=body)
        assert resp.status_code == 401

    async def test_self_register_invalid_token(self, client):
        body = {
            "app_id": "my-app",
            "app_name": "My App",
            "environment": "production",
        }
        # Properly prefixed but not actually in the DB
        resp = await client.post(
            "/api/v1/self-register",
            json=body,
            headers={"X-Modus-TeamToken": "mds_team_" + "x" * 40},
        )
        assert resp.status_code == 401

    async def test_self_register_authorization_header(self, client):
        body = {"app_id": "x", "app_name": "X", "environment": "production"}
        resp = await client.post(
            "/api/v1/self-register",
            json=body,
            headers={"Authorization": "Bearer wrongprefix_xxx"},
        )
        assert resp.status_code == 401

    def test_make_safe_app_id_helper(self):
        from orchestrator.api.topology import _make_safe_app_id
        assert _make_safe_app_id("My App ID") == "my-app-id"
        assert _make_safe_app_id("UPPER_case_with_underscores") == "upper-case-with-underscores"
        # Empty string falls back to default
        assert _make_safe_app_id("###") == "unnamed-app"
        # Truncation to 128 chars
        long_id = _make_safe_app_id("a" * 200)
        assert len(long_id) <= 128

    def test_snapshot_hash_excludes_volatile_fields(self):
        from orchestrator.api.topology import _snapshot_hash
        snap1 = {
            "runtime": "python",
            "pid": 1234,
            "scanned_at": "2026-04-09T12:00:00Z",
            "k8s_pod_name": "pod-abc",
            "agent_version": "2.0.0",
        }
        snap2 = {
            "runtime": "python",
            "pid": 9999,  # different
            "scanned_at": "2026-05-01T00:00:00Z",  # different
            "k8s_pod_name": "pod-xyz",  # different
            "agent_version": "2.1.0",  # different
        }
        # Both should produce the same hash because volatile fields are excluded
        assert _snapshot_hash(snap1) == _snapshot_hash(snap2)

    def test_snapshot_hash_different_for_different_content(self):
        from orchestrator.api.topology import _snapshot_hash
        snap1 = {"runtime": "python", "platform_name": "linux"}
        snap2 = {"runtime": "node", "platform_name": "linux"}
        assert _snapshot_hash(snap1) != _snapshot_hash(snap2)

    def test_team_token_helpers(self):
        from orchestrator.api.topology import (
            _generate_team_token,
            _hash_team_token,
            _TEAM_TOKEN_PREFIX,
        )
        token = _generate_team_token()
        assert token.startswith(_TEAM_TOKEN_PREFIX)
        assert len(token) > 40

        h = _hash_team_token(token)
        assert isinstance(h, str)
        assert h.startswith("$2b$") or h.startswith("$2a$")  # bcrypt prefix


# ─────────────────────────────────────────────────────────────────────────────
# 11. Policies API — additional paths
# ─────────────────────────────────────────────────────────────────────────────


class TestPoliciesAdditional:
    @pytest_asyncio.fixture
    async def seed(self, db_session):
        from orchestrator.db.models import App, GovernancePolicy, Team
        team_id = str(uuid.uuid4())
        app_id = str(uuid.uuid4())
        team = Team(id=team_id, slug="pol-x-team", name="Pol X Team")
        app = App(
            id=app_id, team_id=team_id,
            app_id="pol-x-app", app_name="Pol X App",
            environment="production",
            api_key_hash="fake_hash", api_key_prefix="mds_fake",
        )
        policy = GovernancePolicy(
            name="Existing Policy",
            scope="team",
            policy_type="rate_limit",
            effect="deny",
            priority=200,
            team_id=team_id,
            config={"max_calls": 50, "window_seconds": 60},
            is_active=True,
            created_by="seed",
        )
        db_session.add_all([team, app, policy])
        await db_session.flush()
        return {"team_id": team_id, "app_id": app_id, "policy_id": str(policy.id)}

    async def test_validate_token_cap(self, client, seed):
        resp = await client.post(
            "/api/v1/policies",
            json={
                "name": "Token cap",
                "scope": "team",
                "policy_type": "token_cap",
                "effect": "deny",
                "team_id": seed["team_id"],
                "config": {"max_tokens": 100000, "period": "monthly"},
            },
        )
        assert resp.status_code == 201

    async def test_validate_provider_block(self, client, seed):
        resp = await client.post(
            "/api/v1/policies",
            json={
                "name": "Block providers",
                "scope": "platform",
                "policy_type": "provider_block",
                "effect": "deny",
                "config": {"providers": ["openai"]},
            },
        )
        assert resp.status_code == 201

    async def test_validate_environment_block(self, client, seed):
        resp = await client.post(
            "/api/v1/policies",
            json={
                "name": "Block envs",
                "scope": "platform",
                "policy_type": "environment_block",
                "effect": "deny",
                "config": {"environments": ["dev"]},
            },
        )
        assert resp.status_code == 201

    async def test_validate_latency_cap(self, client, seed):
        resp = await client.post(
            "/api/v1/policies",
            json={
                "name": "Latency cap",
                "scope": "team",
                "policy_type": "latency_cap",
                "effect": "warn",
                "team_id": seed["team_id"],
                "config": {"max_ms": 5000},
            },
        )
        assert resp.status_code == 201

    async def test_validate_amplification_gate(self, client, seed):
        resp = await client.post(
            "/api/v1/policies",
            json={
                "name": "Amp gate",
                "scope": "team",
                "policy_type": "amplification_gate",
                "effect": "deny",
                "team_id": seed["team_id"],
                "config": {"max_amplification": 5},
            },
        )
        assert resp.status_code == 201

    async def test_validate_retry_circuit_breaker(self, client, seed):
        resp = await client.post(
            "/api/v1/policies",
            json={
                "name": "Retry CB",
                "scope": "team",
                "policy_type": "retry_circuit_breaker",
                "effect": "deny",
                "team_id": seed["team_id"],
                "config": {"max_retries": 3},
            },
        )
        assert resp.status_code == 201

    async def test_create_policy_invalid_type_pydantic_422(self, client):
        resp = await client.post(
            "/api/v1/policies",
            json={
                "name": "Bad type",
                "scope": "platform",
                "policy_type": "not_a_real_type",
                "effect": "deny",
                "config": {},
            },
        )
        # Pydantic field validator triggers a 422
        assert resp.status_code in (400, 422)

    async def test_export_policies_includes_seeded(self, client, seed):
        resp = await client.get("/api/v1/policies/export")
        assert resp.status_code == 200
        data = resp.json()
        names = [p["name"] for p in data["policies"]]
        assert "Existing Policy" in names

    async def test_policies_apply_dry_run(self, client, seed):
        resp = await client.post(
            "/api/v1/policies/apply",
            json={
                "version": "1",
                "dry_run": True,
                "policies": [
                    {
                        "name": "New Cap",
                        "type": "rate_limit",
                        "scope": "team",
                        "effect": "deny",
                        "team": seed["team_id"],
                        "config": {"max_calls": 99, "window_seconds": 60},
                    }
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "summary" in body

    def test_validate_policy_config_helper_directly(self):
        """Exercise _validate_policy_config helper for additional types."""
        from orchestrator.api.policies import _validate_policy_config

        # Valid configs — should not raise
        _validate_policy_config("rate_limit", {"max_calls": 100, "window_seconds": 60})
        _validate_policy_config("model_allowlist", {"models": ["gpt-4o"]})
        _validate_policy_config("model_denylist", {"models": ["gpt-4-turbo"]})
        _validate_policy_config("provider_block", {"providers": ["openai"]})
        _validate_policy_config("environment_block", {"environments": ["dev"]})
        _validate_policy_config("token_cap", {"max_tokens": 100, "period": "daily"})
        _validate_policy_config("latency_cap", {"max_ms": 1000})
        _validate_policy_config("amplification_gate", {"max_amplification": 3})
        _validate_policy_config("retry_circuit_breaker", {"max_retries": 5})

        # Invalid — missing required
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_policy_config("rate_limit", {"max_calls": 100})  # missing window_seconds
