"""Tests for orchestrator.core.ebpf_engine — software interceptor, engine."""
import time
from unittest.mock import patch


from orchestrator.core.ebpf_engine import (
    AI_PROVIDER_ENDPOINTS,
    AppBudget,
    EBPFBackend,
    EBPFEngine,
    EnforcementAction,
    InterceptEvent,
    SoftwareInterceptor,
    MAX_APPS,
    MAX_PROVIDERS,
)


# ── Enums ────────────────────────────────────────────────────────────────────

class TestEnums:
    def test_ebpf_backend_values(self):
        assert EBPFBackend.BCC == "bcc"
        assert EBPFBackend.LIBBPF == "libbpf"
        assert EBPFBackend.SOFTWARE == "software"
        assert EBPFBackend.DISABLED == "disabled"

    def test_enforcement_action_values(self):
        assert EnforcementAction.ALLOW == "allow"
        assert EnforcementAction.DROP == "drop"
        assert EnforcementAction.REDIRECT == "redirect"
        assert EnforcementAction.LOG == "log"


# ── Data structures ──────────────────────────────────────────────────────────

class TestInterceptEvent:
    def test_creation(self):
        event = InterceptEvent(
            timestamp_ns=1000000,
            pid=1234,
            comm="python3",
            provider="anthropic",
            destination_ip="104.18.12.33",
            destination_port=443,
            model_hint="claude-haiku-4-5-20251001",
            estimated_tokens=500,
            action_taken="allow",
            latency_us=3.2,
        )
        assert event.pid == 1234
        assert event.provider == "anthropic"
        assert event.action_taken == "allow"


class TestAppBudget:
    def test_defaults(self):
        b = AppBudget(app_id="app1", budget_limit_cents=10000)
        assert b.spent_cents == 0
        assert b.call_count == 0
        assert b.reset_interval_seconds == 3600


# ── SoftwareInterceptor ─────────────────────────────────────────────────────

class TestSoftwareInterceptor:
    def test_init(self):
        si = SoftwareInterceptor()
        assert si.is_active is False

    def test_start_stop(self):
        si = SoftwareInterceptor()
        with patch.object(si, "resolve_providers"):
            si.start()
            assert si.is_active is True
            si.stop()
            assert si.is_active is False

    def test_set_and_check_budget_allow(self):
        si = SoftwareInterceptor()
        budget = AppBudget(
            app_id="app1",
            budget_limit_cents=10000,
            last_reset_epoch=int(time.time()),
        )
        si.set_budget("app1", budget)
        action, reason = si.check_budget("app1", 100)
        assert action == EnforcementAction.ALLOW
        assert "within budget" in reason

    def test_check_budget_no_budget(self):
        si = SoftwareInterceptor()
        action, reason = si.check_budget("unknown")
        assert action == EnforcementAction.ALLOW
        assert "no budget" in reason

    def test_check_budget_exceeded(self):
        si = SoftwareInterceptor()
        budget = AppBudget(
            app_id="app1",
            budget_limit_cents=100,
            spent_cents=90,
            last_reset_epoch=int(time.time()),
        )
        si.set_budget("app1", budget)
        action, reason = si.check_budget("app1", 20)
        assert action == EnforcementAction.DROP
        assert "exceeded" in reason

    def test_check_budget_auto_reset(self):
        si = SoftwareInterceptor()
        budget = AppBudget(
            app_id="app1",
            budget_limit_cents=1000,
            spent_cents=500,
            last_reset_epoch=int(time.time()) - 7200,
            reset_interval_seconds=3600,
        )
        si.set_budget("app1", budget)
        action, reason = si.check_budget("app1", 100)
        assert action == EnforcementAction.ALLOW
        # Budget should have been reset
        assert si.get_budget("app1").spent_cents == 100

    def test_record_event(self):
        si = SoftwareInterceptor()
        event = InterceptEvent(
            timestamp_ns=1000,
            pid=1,
            comm="test",
            provider="openai",
            destination_ip="1.2.3.4",
            destination_port=443,
            model_hint="gpt-4",
            estimated_tokens=100,
            action_taken="allow",
            latency_us=5.0,
        )
        si.record_event(event)
        events = si.get_recent_events()
        assert len(events) == 1

    def test_get_recent_events_limit(self):
        si = SoftwareInterceptor()
        for i in range(10):
            event = InterceptEvent(
                timestamp_ns=i,
                pid=1,
                comm="test",
                provider="openai",
                destination_ip="1.2.3.4",
                destination_port=443,
                model_hint="gpt-4",
                estimated_tokens=100,
                action_taken="allow",
                latency_us=1.0,
            )
            si.record_event(event)
        events = si.get_recent_events(limit=5)
        assert len(events) == 5

    def test_get_stats(self):
        si = SoftwareInterceptor()
        stats = si.get_stats()
        assert "intercepted" in stats
        assert "allowed" in stats
        assert "dropped" in stats
        assert stats["avg_latency_us"] == 0.0

    def test_get_budget_none(self):
        si = SoftwareInterceptor()
        assert si.get_budget("nonexistent") is None


# ── EBPFEngine ───────────────────────────────────────────────────────────────

class TestEBPFEngine:
    @patch("orchestrator.core.ebpf_engine.platform")
    def test_detect_backend_non_linux(self, mock_platform):
        mock_platform.system.return_value = "Windows"
        engine = EBPFEngine()
        assert engine._backend == EBPFBackend.SOFTWARE

    def test_engine_init(self):
        engine = EBPFEngine()
        assert engine._active is False

    def test_start_software_fallback(self):
        engine = EBPFEngine()
        engine._backend = EBPFBackend.SOFTWARE
        with patch.object(engine._software, "resolve_providers"):
            result = engine.start()
            assert engine._active is True
            assert result is False  # fell back to software

    def test_stop(self):
        engine = EBPFEngine()
        engine._backend = EBPFBackend.SOFTWARE
        with patch.object(engine._software, "resolve_providers"):
            engine.start()
            engine.stop()
            assert engine._active is False

    def test_start_already_active(self):
        engine = EBPFEngine()
        engine._active = True
        result = engine.start()
        assert result is True


# ── Constants ────────────────────────────────────────────────────────────────

class TestConstants:
    def test_provider_endpoints(self):
        assert "openai" in AI_PROVIDER_ENDPOINTS
        assert "anthropic" in AI_PROVIDER_ENDPOINTS
        assert "google" in AI_PROVIDER_ENDPOINTS

    def test_limits(self):
        assert MAX_APPS == 1024
        assert MAX_PROVIDERS == 32
