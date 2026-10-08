"""
Tests for sdk.modus.rewind — RewindRegistry, RewindContext,
report_rewind_event, LIFO ordering, thread safety, and zero-overhead path.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch


from modus.rewind import (
    RewindAction,
    RewindContext,
    RewindRegistry,
    report_rewind_event,
)


# ── RewindContext ─────────────────────────────────────────────────────────────


class TestRewindContext:
    def test_defaults(self):
        ctx = RewindContext(trigger_reason="circuit_breaker")
        assert ctx.trigger_reason == "circuit_breaker"
        assert ctx.session_id is None
        assert ctx.call_sequence == []
        assert ctx.error_detail is None
        assert ctx.tokens_consumed == 0
        assert ctx.cost_consumed == 0.0
        assert ctx.timestamp  # should be a non-empty ISO string

    def test_custom_fields(self):
        ctx = RewindContext(
            trigger_reason="budget_suspension",
            session_id="sess-123",
            call_sequence=[{"model": "gpt-4o", "tokens": 100}],
            error_detail="Budget exceeded",
            tokens_consumed=5000,
            cost_consumed=1.23,
        )
        assert ctx.session_id == "sess-123"
        assert len(ctx.call_sequence) == 1
        assert ctx.tokens_consumed == 5000
        assert ctx.cost_consumed == 1.23

    def test_call_sequence_default_is_independent(self):
        """Each instance should get its own list."""
        a = RewindContext(trigger_reason="manual")
        b = RewindContext(trigger_reason="manual")
        a.call_sequence.append({"x": 1})
        assert b.call_sequence == []


# ── RewindRegistry — registration / unregistration ────────────────────────────


class TestRewindRegistryBasic:
    def test_empty_registry_has_no_hooks(self):
        reg = RewindRegistry()
        assert reg.has_hooks is False

    def test_register_single_hook(self):
        reg = RewindRegistry()
        reg.register("db", lambda ctx: True)
        assert reg.has_hooks is True

    def test_unregister_removes_hook(self):
        reg = RewindRegistry()
        reg.register("db", lambda ctx: True)
        reg.unregister("db")
        assert reg.has_hooks is False

    def test_unregister_nonexistent_is_noop(self):
        reg = RewindRegistry()
        reg.unregister("nothing")  # should not raise
        assert reg.has_hooks is False

    def test_register_same_name_replaces(self):
        reg = RewindRegistry()
        calls = []
        reg.register("db", lambda ctx: calls.append("first"))
        reg.register("db", lambda ctx: calls.append("second"))

        ctx = RewindContext(trigger_reason="manual")
        reg.trigger(ctx)
        assert calls == ["second"]

    def test_register_same_name_does_not_duplicate_order(self):
        """Re-registering should not add a second entry to _hook_order."""
        reg = RewindRegistry()
        reg.register("db", lambda ctx: True)
        reg.register("db", lambda ctx: True)
        assert len(reg._hook_order) == 1


# ── LIFO execution order ─────────────────────────────────────────────────────


class TestRewindLIFO:
    def test_lifo_order(self):
        reg = RewindRegistry()
        order = []
        reg.register("first", lambda ctx: order.append("first"))
        reg.register("second", lambda ctx: order.append("second"))
        reg.register("third", lambda ctx: order.append("third"))

        ctx = RewindContext(trigger_reason="circuit_breaker")
        actions = reg.trigger(ctx)

        assert order == ["third", "second", "first"]
        assert len(actions) == 3
        assert actions[0].tool_name == "third"
        assert actions[1].tool_name == "second"
        assert actions[2].tool_name == "first"

    def test_lifo_after_unregister_middle(self):
        reg = RewindRegistry()
        order = []
        reg.register("a", lambda ctx: order.append("a"))
        reg.register("b", lambda ctx: order.append("b"))
        reg.register("c", lambda ctx: order.append("c"))
        reg.unregister("b")

        ctx = RewindContext(trigger_reason="manual")
        reg.trigger(ctx)
        assert order == ["c", "a"]


# ── Successful and failing hooks ──────────────────────────────────────────────


class TestRewindHookResults:
    def test_successful_hook_returns_action_success_true(self):
        reg = RewindRegistry()
        reg.register("db", lambda ctx: True)

        ctx = RewindContext(trigger_reason="manual")
        actions = reg.trigger(ctx)
        assert len(actions) == 1
        assert actions[0].success is True
        assert actions[0].error is None
        assert actions[0].duration_ms >= 0

    def test_hook_returning_false(self):
        reg = RewindRegistry()
        reg.register("db", lambda ctx: False)

        ctx = RewindContext(trigger_reason="manual")
        actions = reg.trigger(ctx)
        assert actions[0].success is False
        assert actions[0].error is None  # no exception, just falsy return

    def test_hook_raising_exception(self):
        reg = RewindRegistry()
        def boom(ctx):
            raise RuntimeError("rollback failed")
        reg.register("db", boom)

        ctx = RewindContext(trigger_reason="circuit_breaker")
        actions = reg.trigger(ctx)
        assert len(actions) == 1
        assert actions[0].success is False
        assert "rollback failed" in actions[0].error

    def test_one_failure_does_not_stop_others(self):
        reg = RewindRegistry()
        order = []

        def fail_hook(ctx):
            raise ValueError("fail")

        reg.register("a", lambda ctx: order.append("a") or True)
        reg.register("b", fail_hook)
        reg.register("c", lambda ctx: order.append("c") or True)

        ctx = RewindContext(trigger_reason="manual")
        actions = reg.trigger(ctx)

        # All three executed (LIFO: c, b, a)
        assert len(actions) == 3
        assert actions[0].tool_name == "c"
        assert actions[0].success is True
        assert actions[1].tool_name == "b"
        assert actions[1].success is False
        assert actions[2].tool_name == "a"
        assert actions[2].success is True

    def test_hook_receives_context_fields(self):
        reg = RewindRegistry()
        received = {}

        def capture(ctx):
            received.update(ctx)
            return True

        reg.register("capture", capture)
        ctx = RewindContext(
            trigger_reason="budget_suspension",
            session_id="s1",
            error_detail="over budget",
            tokens_consumed=999,
            cost_consumed=5.5,
        )
        reg.trigger(ctx)

        assert received["trigger_reason"] == "budget_suspension"
        assert received["session_id"] == "s1"
        assert received["error_detail"] == "over budget"
        assert received["tokens_consumed"] == 999
        assert received["cost_consumed"] == 5.5


# ── History ───────────────────────────────────────────────────────────────────


class TestRewindHistory:
    def test_history_records_trigger(self):
        reg = RewindRegistry()
        reg.register("db", lambda ctx: True)

        ctx = RewindContext(trigger_reason="manual", session_id="sess-1")
        reg.trigger(ctx)

        history = reg.history
        assert len(history) == 1
        assert history[0]["trigger_reason"] == "manual"
        assert history[0]["session_id"] == "sess-1"
        assert len(history[0]["actions"]) == 1

    def test_history_capped_at_max(self):
        reg = RewindRegistry()
        reg._max_history = 5
        reg.register("db", lambda ctx: True)

        for i in range(10):
            ctx = RewindContext(trigger_reason="manual", session_id=f"s{i}")
            reg.trigger(ctx)

        assert len(reg.history) == 5
        # Most recent should be the last ones
        assert reg.history[-1]["session_id"] == "s9"
        assert reg.history[0]["session_id"] == "s5"


# ── Zero overhead when no hooks registered ────────────────────────────────────


class TestZeroOverhead:
    def test_trigger_with_no_hooks_returns_empty(self):
        reg = RewindRegistry()
        ctx = RewindContext(trigger_reason="manual")
        actions = reg.trigger(ctx)
        assert actions == []

    def test_trigger_with_no_hooks_is_fast(self):
        reg = RewindRegistry()
        ctx = RewindContext(trigger_reason="manual")

        start = time.perf_counter()
        for _ in range(10000):
            reg.trigger(ctx)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # 10k triggers should take well under 500ms even on slow hardware
        assert elapsed_ms < 500, f"10k no-op triggers took {elapsed_ms:.1f}ms"

    def test_has_hooks_is_false_when_empty(self):
        reg = RewindRegistry()
        assert reg.has_hooks is False


# ── Thread safety (basic) ────────────────────────────────────────────────────


class TestRewindThreadSafety:
    def test_concurrent_register_and_trigger(self):
        reg = RewindRegistry()
        errors = []

        def register_hooks():
            try:
                for i in range(50):
                    reg.register(f"hook-{threading.current_thread().name}-{i}",
                                 lambda ctx: True)
            except Exception as e:
                errors.append(e)

        def trigger_hooks():
            try:
                for _ in range(50):
                    ctx = RewindContext(trigger_reason="manual")
                    reg.trigger(ctx)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=register_hooks, name=f"reg-{i}"))
            threads.append(threading.Thread(target=trigger_hooks, name=f"trig-{i}"))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread safety errors: {errors}"

    def test_concurrent_unregister(self):
        reg = RewindRegistry()
        for i in range(20):
            reg.register(f"hook-{i}", lambda ctx: True)

        errors = []

        def unregister_some(start):
            try:
                for i in range(start, min(start + 5, 20)):
                    reg.unregister(f"hook-{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=unregister_some, args=(0,)),
            threading.Thread(target=unregister_some, args=(5,)),
            threading.Thread(target=unregister_some, args=(10,)),
            threading.Thread(target=unregister_some, args=(15,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []


# ── report_rewind_event ───────────────────────────────────────────────────────


class TestReportRewindEvent:
    def test_returns_false_when_url_is_empty(self):
        ctx = RewindContext(trigger_reason="manual")
        result = report_rewind_event(
            orchestrator_url="",
            api_key="key",
            app_id="app1",
            team_id="team1",
            context=ctx,
            actions=[],
        )
        assert result is False

    def test_returns_false_when_url_is_none(self):
        ctx = RewindContext(trigger_reason="manual")
        # None is falsy, same early-return branch
        result = report_rewind_event(
            orchestrator_url=None,
            api_key="key",
            app_id="app1",
            team_id="team1",
            context=ctx,
            actions=[],
        )
        assert result is False

    @patch("modus.rewind.urllib_request.urlopen")
    def test_successful_post(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ctx = RewindContext(
            trigger_reason="circuit_breaker",
            session_id="s1",
            tokens_consumed=100,
            cost_consumed=0.5,
        )
        actions = [
            RewindAction(tool_name="db", success=True, duration_ms=1.2),
        ]

        result = report_rewind_event(
            orchestrator_url="http://localhost:8000",
            api_key="test-key",
            app_id="app1",
            team_id="team1",
            context=ctx,
            actions=actions,
        )
        assert result is True
        mock_urlopen.assert_called_once()

        # Verify the request was built correctly
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://localhost:8000/api/v1/governance/rewind-event"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == "Bearer test-key"

    @patch("modus.rewind.urllib_request.urlopen")
    def test_network_error_returns_false(self, mock_urlopen):
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("Connection refused")

        ctx = RewindContext(trigger_reason="manual")
        result = report_rewind_event(
            orchestrator_url="http://localhost:8000",
            api_key="key",
            app_id="app1",
            team_id="team1",
            context=ctx,
            actions=[],
        )
        assert result is False

    @patch("modus.rewind.urllib_request.urlopen")
    def test_timeout_returns_false(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")

        ctx = RewindContext(trigger_reason="manual")
        result = report_rewind_event(
            orchestrator_url="http://localhost:9999",
            api_key="key",
            app_id="app1",
            team_id="team1",
            context=ctx,
            actions=[],
        )
        assert result is False

    @patch("modus.rewind.urllib_request.urlopen")
    def test_trailing_slash_stripped_from_url(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ctx = RewindContext(trigger_reason="manual")
        report_rewind_event(
            orchestrator_url="http://localhost:8000/",
            api_key="key",
            app_id="app1",
            team_id="team1",
            context=ctx,
            actions=[],
        )

        req = mock_urlopen.call_args[0][0]
        assert "//" not in req.full_url.replace("http://", "")
