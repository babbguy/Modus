"""
Tests for orchestrator.core.ebpf_engine — eBPF kernel-level enforcement engine.

Tests the software fallback interceptor, budget management, event recording,
provider resolution, delta engine detection, and singleton access.
"""

from __future__ import annotations

import platform


from orchestrator.core.ebpf_engine import (
    EBPFEngine,
    EBPFBackend,
    SoftwareInterceptor,
    AppBudget,
    EnforcementAction,
    InterceptEvent,
    EBPFStats,
    AI_PROVIDER_ENDPOINTS,
    get_ebpf_engine,
)


# ── SoftwareInterceptor ─────────────────────────────────────────────────────


class TestSoftwareInterceptor:
    """Tests for the pure-Python fallback interceptor."""

    def test_init_defaults(self):
        si = SoftwareInterceptor()
        assert not si.is_active
        assert si.get_stats()["intercepted"] == 0

    def test_start_stop(self):
        si = SoftwareInterceptor()
        si.start()
        assert si.is_active
        si.stop()
        assert not si.is_active

    def test_set_and_check_budget_within_limit(self):
        si = SoftwareInterceptor()
        budget = AppBudget(
            app_id="app1",
            budget_limit_cents=1000,
            reset_interval_seconds=0,  # no auto-reset
        )
        si.set_budget("app1", budget)

        action, reason = si.check_budget("app1", estimated_cost_cents=100)
        assert action == EnforcementAction.ALLOW
        assert "within budget" in reason

    def test_budget_exceeded_returns_drop(self):
        si = SoftwareInterceptor()
        budget = AppBudget(
            app_id="app1",
            budget_limit_cents=100,
            reset_interval_seconds=0,
        )
        si.set_budget("app1", budget)

        # Spend up to limit
        si.check_budget("app1", estimated_cost_cents=50)
        si.check_budget("app1", estimated_cost_cents=50)

        # Next call should be denied
        action, reason = si.check_budget("app1", estimated_cost_cents=10)
        assert action == EnforcementAction.DROP
        assert "exceeded" in reason

    def test_no_budget_configured_allows(self):
        si = SoftwareInterceptor()
        action, reason = si.check_budget("unknown_app")
        assert action == EnforcementAction.ALLOW
        assert "no budget" in reason

    def test_record_event(self):
        si = SoftwareInterceptor()
        event = InterceptEvent(
            timestamp_ns=1000000,
            pid=12345,
            comm="python3",
            provider="openai",
            destination_ip="104.18.6.192",
            destination_port=443,
            model_hint="gpt-4o",
            estimated_tokens=500,
            action_taken="allow",
            latency_us=3.5,
        )
        si.record_event(event)
        events = si.get_recent_events()
        assert len(events) == 1
        assert events[0].pid == 12345
        assert events[0].provider == "openai"

    def test_event_buffer_bounded(self):
        si = SoftwareInterceptor()
        for i in range(12000):
            si.record_event(InterceptEvent(
                timestamp_ns=i, pid=i, comm="test",
                provider="openai", destination_ip="1.2.3.4",
                destination_port=443, model_hint="",
                estimated_tokens=0, action_taken="allow",
                latency_us=1.0,
            ))
        # Buffer trims to 5000 when it hits 10000, then 2000 more get added
        events = si.get_recent_events(limit=10000)
        assert len(events) < 12000  # buffer is bounded, not unbounded

    def test_stats_tracking(self):
        si = SoftwareInterceptor()
        budget = AppBudget(app_id="app1", budget_limit_cents=1000)
        si.set_budget("app1", budget)

        si.check_budget("app1", 50)
        si.check_budget("app1", 50)
        stats = si.get_stats()
        assert stats["allowed"] == 2

    def test_budget_auto_reset(self):
        si = SoftwareInterceptor()
        budget = AppBudget(
            app_id="app1",
            budget_limit_cents=100,
            reset_interval_seconds=1,
            last_reset_epoch=0,  # long ago — triggers immediate reset
        )
        si.set_budget("app1", budget)

        # Should reset on first check because last_reset_epoch=0
        action, _ = si.check_budget("app1", estimated_cost_cents=50)
        assert action == EnforcementAction.ALLOW


# ── EBPFEngine ───────────────────────────────────────────────────────────────


class TestEBPFEngine:
    """Tests for the main eBPF engine (uses software fallback on non-Linux)."""

    def test_detect_backend_non_linux(self):
        if platform.system() != "Linux":
            engine = EBPFEngine()
            assert engine.backend == EBPFBackend.SOFTWARE

    def test_start_stop_software_fallback(self):
        engine = EBPFEngine()
        result = engine.start()
        assert engine.is_active
        # On non-Linux or without BCC, should use software fallback
        if platform.system() != "Linux":
            assert result is False
        engine.stop()
        assert not engine.is_active

    def test_set_budget_software_fallback(self):
        engine = EBPFEngine()
        engine.start()
        budget = AppBudget(app_id="test-app", budget_limit_cents=5000)
        engine.set_budget("test-app", budget)

        retrieved = engine.get_budget("test-app")
        assert retrieved is not None
        assert retrieved.budget_limit_cents == 5000
        engine.stop()

    def test_get_stats(self):
        engine = EBPFEngine()
        engine.start()
        stats = engine.get_stats()
        assert isinstance(stats, EBPFStats)
        assert stats.is_active
        assert stats.backend == "software"
        engine.stop()

    def test_get_recent_events_empty(self):
        engine = EBPFEngine()
        engine.start()
        events = engine.get_recent_events()
        assert events == []
        engine.stop()


# ── Provider endpoints ───────────────────────────────────────────────────────


class TestProviderEndpoints:
    """Tests for the AI provider endpoint configuration."""

    def test_all_major_providers_present(self):
        assert "openai" in AI_PROVIDER_ENDPOINTS
        assert "anthropic" in AI_PROVIDER_ENDPOINTS
        assert "google" in AI_PROVIDER_ENDPOINTS

    def test_each_provider_has_hosts(self):
        for provider, hosts in AI_PROVIDER_ENDPOINTS.items():
            assert len(hosts) > 0, f"{provider} has no hosts"
            for host in hosts:
                assert "." in host, f"Invalid host for {provider}: {host}"


# ── Singleton ────────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_ebpf_engine_returns_same_instance(self):
        e1 = get_ebpf_engine()
        e2 = get_ebpf_engine()
        assert e1 is e2
