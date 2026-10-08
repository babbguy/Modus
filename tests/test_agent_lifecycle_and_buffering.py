"""
Deep agent.py interceptor & lifecycle coverage.

Targets:
  - Heartbeat/sync (_send_heartbeat, _sync_policies, _handle_heartbeat_response)
  - Session/span management (_SessionCtx, _SpanCtx, build_prompt)
  - Aggregation (_aggregate_record, _flush_aggregated, _flush_raw)
  - Provider wrappers (Anthropic sync/stream, OpenAI sync/stream, Bedrock, Google, Groq, Mistral, Cohere)
  - Routing intercept (_try_route_sync, _try_route_async, _extract_prompts_for_routing, _extract_response_text)
  - Utility: _ReusableBody, _BedrockStreamWrapper, _log_routing_outcome, _flush_routing_outcomes
  - build_prompt decorator/context manager
  - _self_register, _report_topology, _rescan_loop
"""
from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


# ── Helper: Minimal agent without network/threads ────────────────────────────

def _make_agent(**overrides):
    from modus.agent import ModusAgent
    defaults = dict(
        orchestrator_url="http://localhost:9999",
        team_token="test-token",
        app_id="test-app",
        fail_open=True,
        flush_interval=999999,
        auto_optimize=False,
        response_cache_enabled=False,
        aggregation_enabled=False,
    )
    defaults.update(overrides)
    agent = ModusAgent(**defaults)
    agent._started = True
    agent._app_id = "test-app"
    agent._team_id = "test-team"
    agent._api_key = "test-key"
    return agent


# ═══════════════════════════════════════════════════════════════════════════════
# 1. HEARTBEAT / SYNC
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeartbeat:
    def test_send_heartbeat_posts_to_orchestrator(self):
        agent = _make_agent()
        agent._instrumented = ["openai", "anthropic"]
        agent._sdk_versions = {"openai": "1.0", "anthropic": "0.5"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("modus.agent._urlopen_tls", return_value=mock_resp) as m:
            agent._send_heartbeat()
            m.assert_called_once()
            req = m.call_args[0][0]
            assert "/api/v1/heartbeat" in req.full_url
            body = json.loads(req.data)
            assert body["instrumented_providers"] == ["openai", "anthropic"]

    def test_send_heartbeat_no_api_key(self):
        agent = _make_agent()
        agent._api_key = ""
        with patch("modus.agent._urlopen_tls") as m:
            agent._send_heartbeat()
            m.assert_not_called()

    def test_send_heartbeat_exception_swallowed(self):
        agent = _make_agent()
        with patch("modus.agent._urlopen_tls", side_effect=ConnectionError("down")):
            agent._send_heartbeat()  # should not raise

    def test_handle_heartbeat_response_with_routing(self):
        agent = _make_agent()
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "routing_fingerprints": [{"hash": "abc", "cheap_model": "haiku"}]
        }).encode()
        with patch("modus.routing_interceptor.update_routing_table") as m:
            agent._handle_heartbeat_response(resp)
            m.assert_called_once_with([{"hash": "abc", "cheap_model": "haiku"}])

    def test_handle_heartbeat_empty_routing(self):
        agent = _make_agent()
        resp = MagicMock()
        resp.read.return_value = json.dumps({}).encode()
        agent._handle_heartbeat_response(resp)  # no error


# ═══════════════════════════════════════════════════════════════════════════════
# 2. POLICY SYNC
# ═══════════════════════════════════════════════════════════════════════════════

class TestPolicySync:
    def test_sync_policies_success(self):
        agent = _make_agent()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "policies": [{"name": "budget_cap", "decision": "deny"}]
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("modus.agent._urlopen_tls", return_value=mock_resp):
            agent._sync_policies()
            assert len(agent._local_policies) == 1
            assert agent._last_policy_sync > 0

    def test_sync_policies_no_api_key(self):
        agent = _make_agent()
        agent._api_key = ""
        agent._sync_policies()
        assert agent._local_policies == []

    def test_sync_policies_exception(self):
        agent = _make_agent()
        with patch("modus.agent._urlopen_tls", side_effect=ConnectionError):
            agent._sync_policies()  # should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# 3. SELF-REGISTER
# ═══════════════════════════════════════════════════════════════════════════════

class TestSelfRegister:
    def test_register_success(self):
        agent = _make_agent()
        agent._api_key = ""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "api_key": "ak_test",
            "app_uuid": "uuid-1",
            "app_id": "my-app",
            "team_id": "team-1",
            "team_slug": "engineering",
            "registered": True,
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("modus.agent._urlopen_tls", return_value=mock_resp):
            result = agent._self_register()
            assert result is True
            assert agent._api_key == "ak_test"
            assert agent._app_id == "my-app"
            assert agent._team_id == "team-1"

    def test_register_failure(self):
        agent = _make_agent()
        with patch("modus.agent._urlopen_tls", side_effect=ConnectionError("refused")):
            result = agent._self_register()
            assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SESSION / SPAN MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionSpan:
    def test_session_context_pushes_and_pops(self):
        agent = _make_agent()
        with agent.session("sess-1") as sess:
            stack = agent._get_span_stack()
            assert len(stack) == 1
            assert stack[0]["session_id"] == "sess-1"
            assert sess.id == "sess-1"
        assert len(agent._get_span_stack()) == 0

    def test_session_auto_id(self):
        agent = _make_agent()
        with agent.session() as sess:
            assert len(sess.id) == 32  # hex uuid

    def test_span_within_session(self):
        agent = _make_agent()
        with agent.session("s1"):
            with agent.span("planning") as sp:
                stack = agent._get_span_stack()
                assert len(stack) == 2
                assert stack[1]["name"] == "planning"
                assert stack[1]["session_id"] == "s1"
                assert sp.call_id is not None
            assert len(agent._get_span_stack()) == 1
        assert len(agent._get_span_stack()) == 0

    def test_span_without_session_is_noop(self):
        agent = _make_agent()
        with agent.span("orphan"):
            # No session => span is no-op, stack stays empty
            stack = agent._get_span_stack()
            assert len(stack) == 0

    def test_current_span_meta_no_session(self):
        agent = _make_agent()
        assert agent._current_span_meta() is None

    def test_current_span_meta_with_session(self):
        agent = _make_agent()
        with agent.session("s2"):
            meta = agent._current_span_meta()
            assert meta is not None
            assert meta["mds_session_id"] == "s2"
            assert "mds_call_id" in meta


# ═══════════════════════════════════════════════════════════════════════════════
# 5. BUILD_PROMPT
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildPrompt:
    def test_as_decorator_bare(self):
        agent = _make_agent()

        @agent.build_prompt
        def my_func():
            return 42

        # The function should still work (span is a no-op without session)
        assert my_func() == 42

    def test_as_decorator_with_name(self):
        agent = _make_agent()

        @agent.build_prompt(name="planner")
        def plan():
            return "planned"

        assert plan() == "planned"

    def test_as_context_manager(self):
        agent = _make_agent()
        with agent.session("s3"):
            with agent.build_prompt(name="ctx"):
                stack = agent._get_span_stack()
                # session + span
                assert len(stack) == 2

    def test_build_prompt_callable_name_raises(self):
        agent = _make_agent()
        with pytest.raises(TypeError, match="keyword argument"):
            agent.build_prompt(name=lambda: None)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. AGGREGATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestAggregation:
    def test_aggregate_record_violation_always_sampled(self):
        agent = _make_agent(aggregation_enabled=True)
        from modus.agent import _UsageRecord
        rec = _UsageRecord(
            provider="openai", resource_type="llm_call", model="gpt-4",
            operation="chat", input_tokens=100, output_tokens=50,
            total_tokens=150, input_cost=Decimal("0.01"),
            output_cost=Decimal("0.005"), total_cost=Decimal("0.015"),
            duration_ms=200, timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"_policy_violation": True},
        )
        agent._aggregate_record(rec)
        # Kept at full detail as a trace AND counted in its bucket, so the
        # orchestrator never has to count traces.
        assert len(agent._agg_sampled) == 1
        assert len(agent._agg_buckets) == 1
        assert next(iter(agent._agg_buckets.values())).call_count == 1

    def test_aggregate_record_normal_into_bucket(self):
        agent = _make_agent(aggregation_enabled=True)
        from modus.agent import _UsageRecord
        rec = _UsageRecord(
            provider="openai", resource_type="llm_call", model="gpt-4",
            operation="chat", input_tokens=100, output_tokens=50,
            total_tokens=150, input_cost=Decimal("0.01"),
            output_cost=Decimal("0.005"), total_cost=Decimal("0.015"),
            duration_ms=200, timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=None,
        )
        agent._aggregate_record(rec)
        assert len(agent._agg_buckets) == 1
        key = f"{rec.timestamp[:13]}|openai:gpt-4:chat:llm_call"
        assert agent._agg_buckets[key].call_count == 1

    def test_aggregate_record_error_always_sampled(self):
        agent = _make_agent(aggregation_enabled=True)
        from modus.agent import _UsageRecord
        rec = _UsageRecord(
            provider="anthropic", resource_type="llm_call", model="claude",
            operation="msg", input_tokens=50, output_tokens=20,
            total_tokens=70, input_cost=None, output_cost=None,
            total_cost=Decimal("0"), duration_ms=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"_error": True},
        )
        agent._aggregate_record(rec)
        assert len(agent._agg_sampled) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 7. FLUSH RAW & AGGREGATED
# ═══════════════════════════════════════════════════════════════════════════════

class TestFlush:
    def test_flush_raw_sends_records(self):
        agent = _make_agent()
        from modus.agent import _UsageRecord
        agent._records.append(_UsageRecord(
            provider="openai", resource_type="llm_call", model="gpt-4",
            operation="chat", input_tokens=10, output_tokens=5,
            total_tokens=15, input_cost=None, output_cost=None,
            total_cost=Decimal("0.001"), duration_ms=50,
            timestamp="2026-01-01T00:00:00Z", metadata=None,
        ))

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"accepted": 1}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("modus.agent._urlopen_tls", return_value=mock_resp) as m:
            agent._flush_raw()
            m.assert_called_once()
            req = m.call_args[0][0]
            body = json.loads(req.data)
            assert len(body["records"]) == 1
            assert body["records"][0]["provider"] == "openai"

    def test_flush_raw_requeues_on_failure(self):
        agent = _make_agent()
        from modus.agent import _UsageRecord
        agent._records.append(_UsageRecord(
            provider="openai", resource_type="llm_call", model="gpt-4",
            operation="chat", input_tokens=10, output_tokens=5,
            total_tokens=15, input_cost=None, output_cost=None,
            total_cost=Decimal("0"), duration_ms=0,
            timestamp="now", metadata=None,
        ))
        with patch("modus.agent._urlopen_tls", side_effect=ConnectionError):
            agent._flush_raw()
        # Kept as a frozen pending batch (same batch_id on retry), not
        # re-buffered under a new batch_id.
        assert len(agent._records) == 0
        assert len(agent._pending_batches) == 1

    def test_flush_aggregated_sends_buckets(self):
        agent = _make_agent(aggregation_enabled=True)
        from modus.agent import _AggregationBucket
        bucket = _AggregationBucket(
            provider="openai", model="gpt-4",
            operation="chat", resource_type="llm_call",
        )
        bucket.call_count = 5
        bucket.input_tokens = 500
        bucket.total_cost = Decimal("0.05")
        bucket.window_start = "2026-01-01T00:00:00Z"
        bucket.window_end = "2026-01-01T00:01:00Z"
        agent._agg_buckets["openai:gpt-4:chat:llm_call"] = bucket

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"accepted": 5}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("modus.agent._urlopen_tls", return_value=mock_resp) as m:
            agent._flush_aggregated()
            body = json.loads(m.call_args[0][0].data)
            assert body["format"] == "aggregated"
            assert len(body["aggregates"]) == 1
            assert body["aggregates"][0]["call_count"] == 5

    def test_flush_aggregated_requeues_on_failure(self):
        agent = _make_agent(aggregation_enabled=True)
        from modus.agent import _AggregationBucket
        bucket = _AggregationBucket(
            provider="openai", model="gpt-4",
            operation="chat", resource_type="llm_call",
        )
        bucket.call_count = 3
        bucket.input_tokens = 300
        agent._agg_buckets["openai:gpt-4:chat:llm_call"] = bucket

        with patch("modus.agent._urlopen_tls", side_effect=ConnectionError):
            agent._flush_aggregated()
        # Kept as a frozen pending batch for an idempotent retry
        assert agent._agg_buckets == {}
        assert len(agent._pending_batches) == 1
        body = json.loads(agent._pending_batches[0].body)
        assert body["aggregates"][0]["call_count"] == 3

    def test_flush_no_api_key(self):
        agent = _make_agent()
        agent._api_key = ""
        with patch("modus.agent._urlopen_tls") as m:
            agent._flush()
            m.assert_not_called()

    def test_flush_routing_outcomes_sends(self):
        agent = _make_agent()
        agent._routing_outcomes = [{"routed_to": "cheap", "timestamp": "t1"}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("modus.agent._urlopen_tls", return_value=mock_resp):
            agent._flush_routing_outcomes()
        assert len(agent._routing_outcomes) == 0

    def test_flush_routing_outcomes_requeues_on_failure(self):
        agent = _make_agent()
        agent._routing_outcomes = [{"routed_to": "cheap"}]
        with patch("modus.agent._urlopen_tls", side_effect=ConnectionError):
            agent._flush_routing_outcomes()
        assert len(agent._routing_outcomes) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 8. RECORD BUFFER
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecordBuffer:
    def test_record_buffer_overflow(self):
        agent = _make_agent()
        agent._max_buffer_size = 2
        # Fill beyond capacity
        for i in range(4):
            agent.record(provider="openai", model="gpt-4", operation="chat",
                         input_tokens=10, output_tokens=5, duration_ms=10)
        assert len(agent._records) == 2

    def test_record_with_cost_estimation(self):
        agent = _make_agent()
        with patch("modus.pricing.estimate_cost", return_value=(Decimal("0.01"), Decimal("0.005"), Decimal("0.015"))):
            agent.record(provider="openai", model="gpt-4", operation="chat",
                         input_tokens=100, output_tokens=50, duration_ms=200)
        assert len(agent._records) == 1
        assert agent._records[0].total_cost == Decimal("0.015")


# ═══════════════════════════════════════════════════════════════════════════════
# 9. TOPOLOGY / RESCAN
# ═══════════════════════════════════════════════════════════════════════════════

class TestTopology:
    def test_report_topology_success(self):
        agent = _make_agent()
        snapshot = MagicMock()
        snapshot.to_dict.return_value = {"packages": ["openai"]}

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ai_summary": "OpenAI detected"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("modus.agent._urlopen_tls", return_value=mock_resp):
            agent._report_topology(snapshot)

    def test_report_topology_no_api_key(self):
        agent = _make_agent()
        agent._api_key = ""
        agent._report_topology(MagicMock())  # should return early

    def test_report_topology_error_swallowed(self):
        agent = _make_agent()
        with patch("modus.agent._urlopen_tls", side_effect=ConnectionError):
            agent._report_topology(MagicMock(to_dict=MagicMock(return_value={})))


# ═══════════════════════════════════════════════════════════════════════════════
# 10. EXTRACT PROMPTS FOR ROUTING
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractPrompts:
    def test_anthropic_prompts(self):
        from modus.agent import ModusAgent
        sys_p, user_p = ModusAgent._extract_prompts_for_routing(
            "anthropic",
            {
                "system": "You are helpful",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert sys_p == "You are helpful"
        assert user_p == "Hello"

    def test_anthropic_list_system(self):
        from modus.agent import ModusAgent
        sys_p, _ = ModusAgent._extract_prompts_for_routing(
            "anthropic",
            {"system": [{"text": "A"}, {"text": "B"}], "messages": []},
        )
        assert "A" in sys_p and "B" in sys_p

    def test_anthropic_content_list(self):
        from modus.agent import ModusAgent
        _, user_p = ModusAgent._extract_prompts_for_routing(
            "anthropic",
            {"messages": [{"role": "user", "content": [{"text": "hi"}]}]},
        )
        assert user_p == "hi"

    def test_openai_prompts(self):
        from modus.agent import ModusAgent
        sys_p, user_p = ModusAgent._extract_prompts_for_routing(
            "openai",
            {"messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "usr"},
            ]},
        )
        assert sys_p == "sys"
        assert user_p == "usr"

    def test_google_prompts(self):
        from modus.agent import ModusAgent
        sys_p, user_p = ModusAgent._extract_prompts_for_routing(
            "google",
            {"config": {"system_instruction": "be nice"}, "contents": "what?"},
        )
        assert sys_p == "be nice"
        assert user_p == "what?"

    def test_cohere_prompts(self):
        from modus.agent import ModusAgent
        sys_p, user_p = ModusAgent._extract_prompts_for_routing(
            "cohere",
            {"preamble": "You are Coral", "message": "Hi"},
        )
        assert sys_p == "You are Coral"
        assert user_p == "Hi"

    def test_unknown_provider_returns_empty(self):
        from modus.agent import ModusAgent
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("unknown", {})
        assert sys_p == ""
        assert user_p == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 11. EXTRACT RESPONSE TEXT
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractResponseText:
    def test_anthropic_response(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        block = MagicMock()
        block.text = "Hello world"
        resp.content = [block]
        assert ModusAgent._extract_response_text("anthropic", resp) == "Hello world"

    def test_openai_response(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "Hello"
        assert ModusAgent._extract_response_text("openai", resp) == "Hello"

    def test_google_response_text(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        resp.text = "Hi from Gemini"
        assert ModusAgent._extract_response_text("google", resp) == "Hi from Gemini"

    def test_google_response_candidates(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        resp.text = None
        part = MagicMock()
        part.text = "from candidate"
        resp.candidates = [MagicMock()]
        resp.candidates[0].content.parts = [part]
        assert ModusAgent._extract_response_text("google", resp) == "from candidate"

    def test_cohere_response_text(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        resp.text = "Hi from Cohere"
        assert ModusAgent._extract_response_text("cohere", resp) == "Hi from Cohere"

    def test_cohere_response_v2(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        resp.text = None
        block = MagicMock()
        block.text = "v2 msg"
        resp.message.content = [block]
        assert ModusAgent._extract_response_text("cohere", resp) == "v2 msg"

    def test_unknown_provider_stringifies(self):
        from modus.agent import ModusAgent
        assert ModusAgent._extract_response_text("acme", "some text") == "some text"

    def test_none_response(self):
        from modus.agent import ModusAgent
        assert ModusAgent._extract_response_text("openai", None) == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 12. REUSABLE BODY (Bedrock helper)
# ═══════════════════════════════════════════════════════════════════════════════

class TestReusableBody:
    def test_read_full(self):
        from modus.agent import _ReusableBody
        body = _ReusableBody(b'{"hello": "world"}')
        assert body.read() == b'{"hello": "world"}'
        # Can read again
        assert body.read() == b'{"hello": "world"}'

    def test_read_partial(self):
        from modus.agent import _ReusableBody
        body = _ReusableBody(b"abcdef")
        assert body.read(3) == b"abc"


# ═══════════════════════════════════════════════════════════════════════════════
# 13. BEDROCK STREAM WRAPPER
# ═══════════════════════════════════════════════════════════════════════════════

class TestBedrockStreamWrapper:
    def test_stream_captures_usage(self):
        from modus.agent import _BedrockStreamWrapper
        agent = _make_agent()
        events = [
            {"chunk": {"bytes": json.dumps({"text": "hi"}).encode()}},
            {"chunk": {"bytes": json.dumps({
                "amazon-bedrock-invocationMetrics": {
                    "inputTokenCount": 100,
                    "outputTokenCount": 50,
                }
            }).encode()}},
        ]
        t0 = time.perf_counter()
        wrapper = _BedrockStreamWrapper(iter(events), "claude-v2", t0, agent)
        collected = list(wrapper)
        assert len(collected) == 2
        # Verify _rec was called
        assert len(agent._records) == 1
        assert agent._records[0].provider == "bedrock"

    def test_stream_with_usage_field(self):
        from modus.agent import _BedrockStreamWrapper
        agent = _make_agent()
        events = [
            {"chunk": {"bytes": json.dumps({
                "usage": {"input_tokens": 10, "output_tokens": 5}
            }).encode()}},
        ]
        wrapper = _BedrockStreamWrapper(iter(events), "claude-v2", time.perf_counter(), agent)
        list(wrapper)
        assert agent._records[0].input_tokens == 10


# ═══════════════════════════════════════════════════════════════════════════════
# 14. LOG ROUTING OUTCOME
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogRoutingOutcome:
    def test_log_routing_outcome_appends(self):
        agent = _make_agent()
        agent._log_routing_outcome(routed_to="cheap", provider="openai")
        assert len(agent._routing_outcomes) == 1
        assert agent._routing_outcomes[0]["provider"] == "openai"
        assert "timestamp" in agent._routing_outcomes[0]

    def test_log_routing_outcome_overflow(self):
        agent = _make_agent()
        agent._max_buffer_size = 2
        for i in range(5):
            agent._log_routing_outcome(routed_to=f"r{i}")
        assert len(agent._routing_outcomes) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 15. ANTHROPIC INTERCEPTOR (sync + stream)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnthropicInterceptor:
    def _setup_mock_anthropic(self):
        """Create a mock anthropic module for instrumentation."""
        anthropic = types.ModuleType("anthropic")
        anthropic.__version__ = "0.30.0"
        resources = types.ModuleType("anthropic.resources")
        anthropic.resources = resources

        class Messages:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.input_tokens = 100
                resp.usage.output_tokens = 50
                resp.model = "claude-3-sonnet"
                resp.stop_reason = "end_turn"
                return resp

        class AsyncMessages:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.input_tokens = 100
                resp.usage.output_tokens = 50
                resp.model = "claude-3-sonnet"
                return resp

        resources.Messages = Messages
        resources.AsyncMessages = AsyncMessages
        return anthropic

    def test_anthropic_sync_create(self):
        anthropic = self._setup_mock_anthropic()
        agent = _make_agent()
        # Mock enforce to be a pass-through
        agent._enforce_with_retry = MagicMock(return_value="claude-3-sonnet")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict(sys.modules, {"anthropic": anthropic, "anthropic.resources": anthropic.resources}):
            agent._instrument_anthropic()

        # Call the patched create
        sdk_instance = MagicMock()
        resp = anthropic.resources.Messages.create(
            sdk_instance, model="claude-3-sonnet", messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.usage.input_tokens == 100
        assert len(agent._records) == 1
        assert agent._records[0].provider == "anthropic"

    def test_anthropic_sync_stream(self):
        anthropic = self._setup_mock_anthropic()
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="claude-3-sonnet")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)

        # Mock streaming events
        msg_start = MagicMock()
        msg_start.type = "message_start"
        msg_start.message.usage.input_tokens = 50

        msg_delta = MagicMock()
        msg_delta.type = "message_delta"
        msg_delta.usage.output_tokens = 30

        # Replace the orig create to return an iterable
        def stream_create(self_sdk, *args, **kwargs):
            return iter([msg_start, msg_delta])

        anthropic.resources.Messages.create = stream_create

        with patch.dict(sys.modules, {"anthropic": anthropic, "anthropic.resources": anthropic.resources}):
            agent._instrument_anthropic()

        sdk_instance = MagicMock()
        stream = anthropic.resources.Messages.create(
            sdk_instance, model="claude-3-sonnet", stream=True,
            messages=[{"role": "user", "content": "hi"}]
        )
        events = list(stream)
        assert len(events) == 2
        assert len(agent._records) == 1
        assert agent._records[0].operation == "messages.create(stream)"


# ═══════════════════════════════════════════════════════════════════════════════
# 16. OPENAI INTERCEPTOR (sync + stream)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOpenAIInterceptor:
    def _setup_mock_openai(self):
        openai = types.ModuleType("openai")
        openai.__version__ = "1.30.0"
        resources = types.ModuleType("openai.resources")
        chat = types.ModuleType("openai.resources.chat")
        openai.resources = resources
        openai.resources.chat = chat

        class Completions:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.prompt_tokens = 100
                resp.usage.completion_tokens = 50
                resp.model = "gpt-4"
                return resp

        class AsyncCompletions:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.prompt_tokens = 100
                resp.usage.completion_tokens = 50
                resp.model = "gpt-4"
                return resp

        chat.Completions = Completions
        chat.AsyncCompletions = AsyncCompletions
        return openai

    def test_openai_sync_create(self):
        openai = self._setup_mock_openai()
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gpt-4")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict(sys.modules, {
            "openai": openai,
            "openai.resources": openai.resources,
            "openai.resources.chat": openai.resources.chat,
        }):
            agent._instrument_openai()

        sdk_instance = MagicMock()
        sdk_instance._client._base_url = "https://api.openai.com/v1"
        resp = openai.resources.chat.Completions.create(
            sdk_instance, model="gpt-4",
            messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.usage.prompt_tokens == 100
        assert len(agent._records) == 1
        assert agent._records[0].provider == "openai"

    def test_openai_sync_stream_injects_options(self):
        openai = self._setup_mock_openai()
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gpt-4")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)

        chunk = MagicMock()
        chunk.usage.prompt_tokens = 100
        chunk.usage.completion_tokens = 50

        def stream_create(self_sdk, *args, **kwargs):
            # Verify stream_options was injected
            assert kwargs.get("stream_options") == {"include_usage": True}
            return iter([chunk])

        openai.resources.chat.Completions.create = stream_create

        with patch.dict(sys.modules, {
            "openai": openai,
            "openai.resources": openai.resources,
            "openai.resources.chat": openai.resources.chat,
        }):
            agent._instrument_openai()

        sdk_instance = MagicMock()
        sdk_instance._client._base_url = "https://api.openai.com/v1"
        stream = openai.resources.chat.Completions.create(
            sdk_instance, model="gpt-4", stream=True,
            messages=[{"role": "user", "content": "hi"}]
        )
        events = list(stream)
        assert len(events) == 1

    def test_openai_response_cache_hit(self):
        openai = self._setup_mock_openai()
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gpt-4")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        cached_resp = MagicMock()
        agent._cache_check = MagicMock(return_value=cached_resp)

        with patch.dict(sys.modules, {
            "openai": openai,
            "openai.resources": openai.resources,
            "openai.resources.chat": openai.resources.chat,
        }):
            agent._instrument_openai()

        sdk_instance = MagicMock()
        sdk_instance._client._base_url = "https://api.openai.com/v1"
        resp = openai.resources.chat.Completions.create(
            sdk_instance, model="gpt-4",
            messages=[{"role": "user", "content": "hi"}]
        )
        assert resp is cached_resp


# ═══════════════════════════════════════════════════════════════════════════════
# 17. BEDROCK INTERCEPTOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestBedrockInterceptor:
    def _setup_mock_boto3(self):
        boto3 = types.ModuleType("boto3")
        boto3.__version__ = "1.34.0"

        class Session:
            @staticmethod
            def client(service_name, *args, **kwargs):
                c = MagicMock()
                if service_name == "bedrock-runtime":
                    c.invoke_model = MagicMock(return_value={
                        "body": BytesIO(json.dumps({
                            "usage": {"input_tokens": 50, "output_tokens": 20}
                        }).encode()),
                    })
                    c.invoke_model_with_response_stream = MagicMock()
                return c

        boto3.Session = Session
        boto3.client = lambda sn, *a, **kw: Session.client(sn, *a, **kw)
        return boto3

    def test_bedrock_instrumentation_patches_client(self):
        boto3 = self._setup_mock_boto3()
        agent = _make_agent()
        agent.enforce = MagicMock()

        with patch.dict(sys.modules, {"boto3": boto3}):
            agent._instrument_boto3()

        assert "bedrock" in agent._instrumented

    def test_bedrock_session_client_patched(self):
        """Test that Session.client is also patched for bedrock-runtime."""
        boto3 = self._setup_mock_boto3()
        agent = _make_agent()
        agent.enforce = MagicMock()

        with patch.dict(sys.modules, {"boto3": boto3}):
            agent._instrument_boto3()

        # Verify instrumentation was registered
        assert "bedrock" in agent._instrumented
        assert agent._sdk_versions.get("bedrock") == "1.34.0"


# ═══════════════════════════════════════════════════════════════════════════════
# 18. GROQ INTERCEPTOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestGroqInterceptor:
    def _setup_mock_groq(self):
        groq = types.ModuleType("groq")
        groq.__version__ = "0.5.0"
        resources = types.ModuleType("groq.resources")
        chat = types.ModuleType("groq.resources.chat")
        groq.resources = resources
        groq.resources.chat = chat

        class Completions:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.prompt_tokens = 50
                resp.usage.completion_tokens = 25
                resp.model = "llama-3-70b"
                return resp

        class AsyncCompletions:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.prompt_tokens = 50
                resp.usage.completion_tokens = 25
                return resp

        chat.Completions = Completions
        chat.AsyncCompletions = AsyncCompletions
        return groq

    def test_groq_sync_create(self):
        groq = self._setup_mock_groq()
        agent = _make_agent()
        agent.enforce = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict(sys.modules, {
            "groq": groq,
            "groq.resources": groq.resources,
            "groq.resources.chat": groq.resources.chat,
        }):
            agent._instrument_groq()

        assert "groq" in agent._instrumented
        sdk_instance = MagicMock()
        groq.resources.chat.Completions.create(
            sdk_instance, model="llama-3-70b",
            messages=[{"role": "user", "content": "hi"}]
        )
        assert len(agent._records) == 1
        assert agent._records[0].provider == "groq"


# ═══════════════════════════════════════════════════════════════════════════════
# 19. MISTRAL INTERCEPTOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestMistralInterceptor:
    def _setup_mock_mistral_new(self):
        """New SDK (>= 1.0) with Mistral class and resources.chat.Chat."""
        mistralai = types.ModuleType("mistralai")
        mistralai.__version__ = "1.2.0"
        resources = types.ModuleType("mistralai.resources")
        resources_chat = types.ModuleType("mistralai.resources.chat")
        mistralai.resources = resources
        mistralai.resources.chat = resources_chat

        class Mistral:
            pass

        class Chat:
            @staticmethod
            def complete(self_r, *args, **kwargs):
                resp = MagicMock()
                resp.usage.prompt_tokens = 80
                resp.usage.completion_tokens = 40
                resp.model = "mistral-large"
                return resp

        resources_chat.Chat = Chat
        mistralai.Mistral = Mistral

        return mistralai, resources_chat

    def test_mistral_new_sdk(self):
        mistralai, resources_chat = self._setup_mock_mistral_new()
        agent = _make_agent()
        agent.enforce = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict(sys.modules, {
            "mistralai": mistralai,
            "mistralai.resources": mistralai.resources,
            "mistralai.resources.chat": resources_chat,
        }):
            agent._instrument_mistral()

        assert "mistral" in agent._instrumented

    def _setup_mock_mistral_legacy(self):
        """Legacy SDK with MistralClient."""
        mistralai = types.ModuleType("mistralai")
        mistralai.__version__ = "0.3.0"
        client_mod = types.ModuleType("mistralai.client")

        class MistralClient:
            @staticmethod
            def chat(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.prompt_tokens = 80
                resp.usage.completion_tokens = 40
                return resp

        client_mod.MistralClient = MistralClient
        return mistralai, client_mod

    def test_mistral_legacy_sdk(self):
        mistralai, client_mod = self._setup_mock_mistral_legacy()
        agent = _make_agent()
        agent.enforce = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict(sys.modules, {
            "mistralai": mistralai,
            "mistralai.client": client_mod,
        }):
            agent._instrument_mistral()

        assert "mistral" in agent._instrumented


# ═══════════════════════════════════════════════════════════════════════════════
# 20. COHERE INTERCEPTOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestCohereInterceptor:
    def _setup_mock_cohere(self):
        cohere = types.ModuleType("cohere")
        cohere.__version__ = "5.0.0"

        class Client:
            @staticmethod
            def chat(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.tokens.input_tokens = 60
                resp.usage.tokens.output_tokens = 30
                resp.model = "command-r-plus"
                return resp

        class AsyncClient:
            @staticmethod
            async def chat(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage.tokens.input_tokens = 60
                resp.usage.tokens.output_tokens = 30
                return resp

        cohere.Client = Client
        cohere.AsyncClient = AsyncClient
        return cohere

    def test_cohere_sync_create(self):
        cohere = self._setup_mock_cohere()
        agent = _make_agent()
        agent.enforce = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict(sys.modules, {"cohere": cohere}):
            agent._instrument_cohere()

        assert "cohere" in agent._instrumented
        sdk_instance = MagicMock()
        cohere.Client.chat(sdk_instance, model="command-r-plus", message="hi")
        assert len(agent._records) == 1
        assert agent._records[0].provider == "cohere"


# ═══════════════════════════════════════════════════════════════════════════════
# 21. GOOGLE GENAI INTERCEPTOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestGoogleGenAIInterceptor:
    def _setup_mock_google_genai(self):
        google = types.ModuleType("google")
        genai = types.ModuleType("google.genai")
        genai.__version__ = "1.0.0"
        models = types.ModuleType("google.genai.models")
        google.genai = genai

        class Models:
            @staticmethod
            def generate_content(self_m, *args, **kwargs):
                resp = MagicMock()
                resp.usage_metadata.prompt_token_count = 100
                resp.usage_metadata.candidates_token_count = 50
                return resp

        models.Models = Models
        return google, genai, models

    def test_google_new_sdk(self):
        google, genai, models = self._setup_mock_google_genai()
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gemini-pro")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict(sys.modules, {
            "google": google,
            "google.genai": genai,
            "google.genai.models": models,
        }):
            agent._instrument_google_genai()

        assert "gemini" in agent._instrumented

    def _setup_mock_google_legacy(self):
        google = types.ModuleType("google")
        genai = types.ModuleType("google.generativeai")
        genai.__version__ = "0.3.0"
        gm = types.ModuleType("google.generativeai.generative_models")
        google.generativeai = genai

        class GenerativeModel:
            model_name = "gemini-pro"

            @staticmethod
            def generate_content(self_m, *args, **kwargs):
                resp = MagicMock()
                resp.usage_metadata.prompt_token_count = 100
                resp.usage_metadata.candidates_token_count = 50
                return resp

            @staticmethod
            async def generate_content_async(self_m, *args, **kwargs):
                resp = MagicMock()
                resp.usage_metadata.prompt_token_count = 100
                resp.usage_metadata.candidates_token_count = 50
                return resp

        gm.GenerativeModel = GenerativeModel
        return google, genai, gm

    def test_google_legacy_sdk(self):
        google, genai, gm = self._setup_mock_google_legacy()
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gemini-pro")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)

        with patch.dict(sys.modules, {
            "google": google,
            "google.generativeai": genai,
            "google.generativeai.generative_models": gm,
        }):
            agent._instrument_google_genai()

        assert "gemini" in agent._instrumented


# ═══════════════════════════════════════════════════════════════════════════════
# 22. DEEPSEEK INSTRUMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeepSeekInstrument:
    def test_deepseek_covered_via_openai(self):
        agent = _make_agent()
        openai = types.ModuleType("openai")
        openai.__version__ = "1.0"
        deepseek = types.ModuleType("deepseek")

        with patch.dict(sys.modules, {"openai": openai, "deepseek": deepseek}):
            agent._instrument_deepseek()
        # No error, just logs

    def test_deepseek_no_openai(self):
        agent = _make_agent()
        # Ensure openai is not importable
        with patch.dict(sys.modules, {"openai": None}):
            agent._instrument_deepseek()


# ═══════════════════════════════════════════════════════════════════════════════
# 23. SHUTDOWN FLUSH
# ═══════════════════════════════════════════════════════════════════════════════

class TestShutdownFlush:
    def test_shutdown_flush_sets_event(self):
        agent = _make_agent()
        agent._flush_thread = MagicMock()
        agent._shutdown_flush()
        assert agent._shutdown.is_set()
        agent._flush_thread.join.assert_called_once_with(timeout=15)

    def test_shutdown_flush_no_thread(self):
        agent = _make_agent()
        agent._flush_thread = None
        agent._shutdown_flush()
        assert agent._shutdown.is_set()
