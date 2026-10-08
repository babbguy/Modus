"""
Tests for sdk/modus/agent.py — Agent initialization, caches,
local policy enforcement, circuit breaker, usage recording, aggregation,
sessions/spans, and gateway health.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from modus.agent import (
    ModusAgent,
    PolicyViolationError,
    _AggregationBucket,
    _EvaluateCache,
    _ResponseCache,
    _UsageRecord,
    _safe_float,
    _safe_int,
)


# ── Helper: create agent without network ────────────────────────────────────


def _make_agent(**overrides) -> ModusAgent:
    defaults = dict(
        orchestrator_url="http://localhost:9999",
        team_token="mds_team_test",
        fail_open=True,
        flush_interval=999,
        aggregation_enabled=False,
    )
    defaults.update(overrides)
    agent = ModusAgent(**defaults)
    # Pretend we registered so API key is set
    agent._api_key = "mds_test_key"
    agent._app_id = "test-app"
    agent._team_id = "team-1"
    return agent


# ── _safe_int / _safe_float ─────────────────────────────────────────────────


class TestSafeInt:
    def test_default_when_unset(self):
        assert _safe_int("MODUS_TEST_NONEXISTENT_INT_XYZ", 42) == 42

    def test_valid_value(self):
        os.environ["MODUS_TEST_INT_VALID"] = "100"
        try:
            assert _safe_int("MODUS_TEST_INT_VALID", 0) == 100
        finally:
            del os.environ["MODUS_TEST_INT_VALID"]

    def test_invalid_value_returns_default(self):
        os.environ["MODUS_TEST_INT_BAD"] = "not-a-number"
        try:
            assert _safe_int("MODUS_TEST_INT_BAD", 7) == 7
        finally:
            del os.environ["MODUS_TEST_INT_BAD"]


class TestSafeFloat:
    def test_default_when_unset(self):
        assert _safe_float("MODUS_TEST_NONEXISTENT_FLOAT_XYZ", 3.14) == 3.14

    def test_valid_value(self):
        os.environ["MODUS_TEST_FLOAT_VALID"] = "2.5"
        try:
            assert _safe_float("MODUS_TEST_FLOAT_VALID", 0.0) == 2.5
        finally:
            del os.environ["MODUS_TEST_FLOAT_VALID"]

    def test_invalid_value_returns_default(self):
        os.environ["MODUS_TEST_FLOAT_BAD"] = "nope"
        try:
            assert _safe_float("MODUS_TEST_FLOAT_BAD", 1.0) == 1.0
        finally:
            del os.environ["MODUS_TEST_FLOAT_BAD"]


# ── PolicyViolationError ────────────────────────────────────────────────────


class TestPolicyViolationError:
    def test_basic_attributes(self):
        e = PolicyViolationError(
            decision="deny",
            reason="Budget exceeded",
            policy_id="p-1",
            policy_name="daily-cap",
        )
        assert e.decision == "deny"
        assert e.reason == "Budget exceeded"
        assert e.policy_id == "p-1"
        assert e.policy_name == "daily-cap"
        assert "deny" in str(e)

    def test_suggested_model_in_message(self):
        e = PolicyViolationError(
            decision="deny",
            reason="Overspend",
            suggested_model="gpt-4o-mini",
        )
        assert e.suggested_model == "gpt-4o-mini"
        assert "gpt-4o-mini" in str(e)

    def test_retry_after_in_message(self):
        e = PolicyViolationError(
            decision="throttle",
            reason="Rate limit",
            retry_after_seconds=30,
        )
        assert e.retry_after_seconds == 30
        assert "30s" in str(e)

    def test_custom_message_appended(self):
        e = PolicyViolationError(
            decision="deny",
            reason="Blocked",
            message="Contact admin",
        )
        assert "Contact admin" in str(e)


# ── _EvaluateCache ──────────────────────────────────────────────────────────


class TestEvaluateCache:
    def test_set_and_get(self):
        cache = _EvaluateCache(ttl=10.0)
        cache.set("openai", "gpt-4o", "prod", {"decision": "allow"})
        result = cache.get("openai", "gpt-4o", "prod")
        assert result is not None
        assert result["decision"] == "allow"

    def test_miss_returns_none(self):
        cache = _EvaluateCache()
        assert cache.get("openai", "gpt-4o", "prod") is None

    def test_expired_entry_returns_none(self):
        cache = _EvaluateCache(ttl=0.001)
        cache.set("openai", "gpt-4o", "prod", {"decision": "allow"})
        time.sleep(0.01)
        assert cache.get("openai", "gpt-4o", "prod") is None

    def test_max_entries_eviction(self):
        cache = _EvaluateCache(ttl=60.0, max_entries=2)
        cache.set("a", None, "e", {"v": 1})
        cache.set("b", None, "e", {"v": 2})
        cache.set("c", None, "e", {"v": 3})  # evicts "a"
        assert cache.get("a", None, "e") is None
        assert cache.get("b", None, "e") is not None
        assert cache.get("c", None, "e") is not None

    def test_clear(self):
        cache = _EvaluateCache()
        cache.set("x", None, "e", {"v": 1})
        cache.clear()
        assert cache.get("x", None, "e") is None

    def test_different_envs_different_keys(self):
        cache = _EvaluateCache()
        cache.set("openai", "gpt-4", "prod", {"d": "allow"})
        cache.set("openai", "gpt-4", "staging", {"d": "deny"})
        assert cache.get("openai", "gpt-4", "prod")["d"] == "allow"
        assert cache.get("openai", "gpt-4", "staging")["d"] == "deny"


# ── _ResponseCache ──────────────────────────────────────────────────────────


class TestResponseCache:
    def test_put_and_get(self):
        cache = _ResponseCache(max_entries=10, ttl_seconds=60.0)
        cache.put("openai", "gpt-4o", [{"role": "user", "content": "hi"}], "response1")
        result = cache.get("openai", "gpt-4o", [{"role": "user", "content": "hi"}])
        assert result == "response1"

    def test_miss(self):
        cache = _ResponseCache()
        assert cache.get("openai", "gpt-4o", "test") is None

    def test_expired_returns_none(self):
        cache = _ResponseCache(ttl_seconds=0.001)
        cache.put("a", "m", "msg", "resp")
        time.sleep(0.01)
        assert cache.get("a", "m", "msg") is None

    def test_stats(self):
        cache = _ResponseCache()
        cache.get("a", "m", "x")  # miss
        cache.put("a", "m", "x", "r")
        cache.get("a", "m", "x")  # hit
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["entries"] == 1
        assert 0.0 <= stats["hit_rate"] <= 1.0

    def test_max_entries_eviction(self):
        cache = _ResponseCache(max_entries=2, ttl_seconds=60.0)
        cache.put("a", "m", "msg1", "r1")
        cache.put("a", "m", "msg2", "r2")
        cache.put("a", "m", "msg3", "r3")  # evicts msg1
        assert cache.get("a", "m", "msg1") is None
        assert cache.get("a", "m", "msg3") == "r3"

    def test_clear_resets_stats(self):
        cache = _ResponseCache()
        cache.put("a", "m", "x", "r")
        cache.get("a", "m", "x")
        cache.clear()
        stats = cache.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["entries"] == 0

    def test_hash_key_deterministic(self):
        k1 = _ResponseCache._hash_key("openai", "gpt-4o", [{"role": "user"}])
        k2 = _ResponseCache._hash_key("openai", "gpt-4o", [{"role": "user"}])
        assert k1 == k2

    def test_hash_key_differs_on_provider(self):
        k1 = _ResponseCache._hash_key("openai", "gpt-4o", [])
        k2 = _ResponseCache._hash_key("anthropic", "gpt-4o", [])
        assert k1 != k2


# ── _AggregationBucket ──────────────────────────────────────────────────────


class TestAggregationBucket:
    def _make_record(self, **kw) -> _UsageRecord:
        defaults = dict(
            provider="openai", resource_type="llm_call",
            model="gpt-4o", operation="chat",
            input_tokens=100, output_tokens=50, total_tokens=150,
            input_cost=Decimal("0.01"), output_cost=Decimal("0.005"),
            total_cost=Decimal("0.015"), duration_ms=200,
            timestamp="2026-04-09T00:00:00Z",
        )
        defaults.update(kw)
        return _UsageRecord(**defaults)

    def test_accumulate_single(self):
        b = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        r = self._make_record()
        b.accumulate(r)
        assert b.call_count == 1
        assert b.input_tokens == 100
        assert b.output_tokens == 50
        assert b.total_cost == Decimal("0.015")
        assert b.duration_ms_min == 200
        assert b.duration_ms_max == 200

    def test_accumulate_multiple(self):
        b = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        b.accumulate(self._make_record(duration_ms=100))
        b.accumulate(self._make_record(duration_ms=300))
        assert b.call_count == 2
        assert b.input_tokens == 200
        assert b.duration_ms_min == 100
        assert b.duration_ms_max == 300
        assert b.duration_ms_sum == 400

    def test_to_dict(self):
        b = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        b.accumulate(self._make_record())
        d = b.to_dict()
        assert d["provider"] == "openai"
        assert d["call_count"] == 1
        assert d["total_cost"] == "0.015"
        assert d["duration_ms_avg"] == 200

    def test_to_dict_avg_zero_calls(self):
        b = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        d = b.to_dict()
        assert d["duration_ms_avg"] is None

    def test_accumulate_none_tokens(self):
        b = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        r = self._make_record(
            input_tokens=None, output_tokens=None,
            total_tokens=None, duration_ms=None,
            input_cost=None, output_cost=None,
        )
        b.accumulate(r)
        assert b.call_count == 1
        assert b.input_tokens == 0
        assert b.duration_ms_min is None


# ── ModusAgent initialization ────────────────────────────────────────────


class TestAgentInit:
    def test_basic_properties(self):
        agent = _make_agent()
        assert agent.orchestrator_url == "http://localhost:9999"
        assert agent.team_token == "mds_team_test"
        assert agent.fail_open is True
        assert agent._started is False

    def test_from_env_missing_vars_raises(self):
        env = {"MODUS_URL": "", "MODUS_TEAM_TOKEN": ""}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MODUS_URL", None)
            os.environ.pop("MODUS_TEAM_TOKEN", None)
            os.environ.pop("MODUS_ORCHESTRATOR_URL", None)
            with pytest.raises(ValueError, match="Missing"):
                ModusAgent.from_env()

    def test_from_env_success(self):
        env = {
            "MODUS_URL": "http://test:8080",
            "MODUS_TEAM_TOKEN": "mds_team_abc",
        }
        with patch.dict(os.environ, env, clear=False):
            agent = ModusAgent.from_env()
            assert agent.orchestrator_url == "http://test:8080"
            assert agent.team_token == "mds_team_abc"

    def test_url_trailing_slash_stripped(self):
        agent = ModusAgent(
            orchestrator_url="http://localhost:9999/",
            team_token="tok",
        )
        assert agent.orchestrator_url == "http://localhost:9999"

    def test_local_budget_cap_from_constructor(self):
        agent = _make_agent(local_budget_cap_usd=50.0)
        assert agent.local_budget_cap_usd == Decimal("50.0")

    def test_response_cache_enabled(self):
        agent = _make_agent(response_cache_enabled=True)
        assert agent._response_cache is not None

    def test_response_cache_disabled_by_default(self):
        agent = _make_agent()
        assert agent._response_cache is None

    def test_response_cache_stats_none_when_disabled(self):
        agent = _make_agent()
        assert agent.response_cache_stats is None

    def test_response_cache_stats_when_enabled(self):
        agent = _make_agent(response_cache_enabled=True)
        stats = agent.response_cache_stats
        assert stats is not None
        assert "hits" in stats


# ── Local policy enforcement ────────────────────────────────────────────────


class TestLocalPolicyEnforcement:
    def test_no_policies_returns_none(self):
        agent = _make_agent()
        agent._local_policies = []
        result = agent._evaluate_local("openai", "gpt-4o", None, None)
        assert result is None

    def test_model_denylist_blocks(self):
        agent = _make_agent()
        agent._local_policies = [
            {
                "policy_type": "model_denylist",
                "config": {"models": ["gpt-4o"]},
                "effect": "deny",
                "name": "no-gpt4o",
            }
        ]
        result = agent._evaluate_local("openai", "gpt-4o", None, None)
        assert result is not None
        assert result["decision"] == "deny"

    def test_model_allowlist_blocks_unlisted(self):
        agent = _make_agent()
        agent._local_policies = [
            {
                "policy_type": "model_allowlist",
                "config": {"models": ["gpt-4o-mini"]},
                "effect": "deny",
                "name": "only-mini",
            }
        ]
        result = agent._evaluate_local("openai", "gpt-4o", None, None)
        assert result is not None
        assert result["decision"] == "deny"

    def test_model_allowlist_allows_listed(self):
        agent = _make_agent()
        agent._local_policies = [
            {
                "policy_type": "model_allowlist",
                "config": {"models": ["gpt-4o"]},
                "effect": "deny",
                "name": "only-gpt4o",
            }
        ]
        result = agent._evaluate_local("openai", "gpt-4o", None, None)
        assert result is None

    def test_provider_block(self):
        agent = _make_agent()
        agent._local_policies = [
            {
                "policy_type": "provider_block",
                "config": {"providers": ["anthropic"]},
                "effect": "deny",
                "name": "block-anthropic",
            }
        ]
        result = agent._evaluate_local("anthropic", "claude-3", None, None)
        assert result is not None
        assert result["decision"] == "deny"

    def test_environment_block(self):
        agent = _make_agent(environment="staging")
        agent._local_policies = [
            {
                "policy_type": "environment_block",
                "config": {"environments": ["staging"]},
                "effect": "deny",
                "name": "no-staging",
            }
        ]
        result = agent._evaluate_local("openai", "gpt-4o", None, None)
        assert result is not None
        assert result["decision"] == "deny"

    def test_environment_block_does_not_match_other_env(self):
        agent = _make_agent(environment="production")
        agent._local_policies = [
            {
                "policy_type": "environment_block",
                "config": {"environments": ["staging"]},
                "effect": "deny",
                "name": "no-staging",
            }
        ]
        result = agent._evaluate_local("openai", "gpt-4o", None, None)
        assert result is None


# ── Local budget cap ────────────────────────────────────────────────────────


class TestLocalBudgetCap:
    def test_enforce_blocks_when_cap_exceeded(self):
        agent = _make_agent(local_budget_cap_usd=1.0)
        agent._local_spend_usd = Decimal("1.00")
        with pytest.raises(PolicyViolationError, match="cap exceeded"):
            agent.enforce(provider="openai", model="gpt-4o")

    def test_enforce_blocks_projected_breach(self):
        agent = _make_agent(local_budget_cap_usd=1.0)
        agent._local_spend_usd = Decimal("0.90")
        with pytest.raises(PolicyViolationError, match="cap would be exceeded"):
            agent.enforce(
                provider="openai", model="gpt-4o",
                estimated_cost=Decimal("0.20"),
            )

    def test_enforce_allows_under_cap(self):
        agent = _make_agent(local_budget_cap_usd=10.0)
        agent._local_spend_usd = Decimal("0.50")
        # Mock _call_evaluate to return allow
        agent._call_evaluate = lambda *a, **kw: {"decision": "allow"}
        result = agent.enforce(provider="openai", model="gpt-4o")
        assert result is None  # None = allowed, no suggested model

    def test_reset_local_spend(self):
        agent = _make_agent(local_budget_cap_usd=10.0)
        agent._local_spend_usd = Decimal("5.00")
        agent.reset_local_spend()
        assert agent._local_spend_usd == Decimal("0")


# ── Circuit breaker ─────────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_trips_after_threshold(self):
        agent = _make_agent()
        agent._circuit_breaker_threshold = 3
        for _ in range(3):
            agent._gateway_consecutive_failures += 1
            agent._maybe_trip_circuit_breaker()
        assert agent._circuit_breaker_tripped_at is not None

    def test_fail_open_returns_allow(self):
        agent = _make_agent(fail_open=True)
        agent._circuit_breaker_tripped_at = time.monotonic()
        agent._circuit_breaker_cooldown = 60.0
        result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")
        assert result["decision"] == "allow"
        assert "Circuit breaker" in result.get("reason", "")

    def test_fail_closed_returns_deny(self):
        agent = _make_agent(fail_open=False)
        agent._circuit_breaker_tripped_at = time.monotonic()
        agent._circuit_breaker_cooldown = 60.0
        result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")
        assert result["decision"] == "deny"

    def test_half_open_after_cooldown(self):
        agent = _make_agent(fail_open=True)
        agent._circuit_breaker_tripped_at = time.monotonic() - 120
        agent._circuit_breaker_cooldown = 60.0
        # The _call_evaluate will try the request (and fail since no server)
        # but the circuit breaker should be cleared (half-open)
        result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")
        # Should have attempted and failed, then returned fail-open allow
        assert result["decision"] == "allow"


class TestNeverBlockGuarantee:
    """
    The SDK must NEVER take down the customer's application. An unexpected error
    anywhere in the enforcement chain (not a deliberate policy deny) must honour
    fail_open: allow the original call by default, only block when the operator
    has explicitly opted into fail-closed enforcement.
    """

    def test_unexpected_error_fail_open_allows_original_model(self):
        agent = _make_agent(fail_open=True)
        # Simulate a bug anywhere in enforcement (KeyError, AttributeError, etc.)
        with patch.object(agent, "enforce", side_effect=RuntimeError("boom")):
            model = agent._enforce_with_retry("openai", "gpt-4o")
        # Never blocks: returns the ORIGINAL model, call proceeds unmodified.
        assert model == "gpt-4o"

    def test_unexpected_error_fail_closed_raises(self):
        agent = _make_agent(fail_open=False)
        with patch.object(agent, "enforce", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                agent._enforce_with_retry("openai", "gpt-4o")

    def test_policy_violation_propagates_even_when_fail_open(self):
        # A deliberate deny is a REAL decision — fail_open governs unreachable
        # decisions, not explicit denies. It must still block.
        agent = _make_agent(fail_open=True)
        deny = PolicyViolationError(decision="deny", reason="budget exceeded")
        with patch.object(agent, "enforce", side_effect=deny):
            with pytest.raises(PolicyViolationError):
                agent._enforce_with_retry("openai", "gpt-4o")

    def test_auto_optimize_substitute_also_denied_propagates(self):
        # If the server suggests a cheaper model and THAT is also denied, we
        # must not swallow it — the original deny propagates (still blocks).
        agent = _make_agent(fail_open=True)
        agent._auto_optimize = True
        first = PolicyViolationError(
            decision="deny", reason="over cap", suggested_model="gpt-4o-mini")
        second = PolicyViolationError(decision="deny", reason="still over cap")
        with patch.object(agent, "enforce", side_effect=[first, second]):
            with pytest.raises(PolicyViolationError):
                agent._enforce_with_retry("openai", "gpt-4o")


# ── Gateway health properties ───────────────────────────────────────────────


class TestGatewayHealth:
    def test_healthy_when_no_failures(self):
        agent = _make_agent()
        assert agent.gateway_healthy is True

    def test_unhealthy_after_failures(self):
        agent = _make_agent()
        agent._gateway_consecutive_failures = 5
        agent._gateway_last_success = time.monotonic() - 120
        assert agent.gateway_healthy is False

    def test_gateway_status_dict(self):
        agent = _make_agent()
        status = agent.gateway_status
        assert "healthy" in status
        assert "consecutive_failures" in status
        assert "local_spend_usd" in status
        assert "fail_open" in status


# ── Session & Span tracking ────────────────────────────────────────────────


class TestSessionSpan:
    def test_session_context_manager(self):
        agent = _make_agent()
        with agent.session(session_id="test-session") as sess:
            assert sess.id == "test-session"
            stack = agent._get_span_stack()
            assert len(stack) == 1
            assert stack[0]["session_id"] == "test-session"
        # Stack should be empty after exit
        assert len(agent._get_span_stack()) == 0

    def test_session_auto_id(self):
        agent = _make_agent()
        with agent.session() as sess:
            assert sess.id is not None
            assert len(sess.id) > 0

    def test_span_within_session(self):
        agent = _make_agent()
        with agent.session(session_id="s1"):
            with agent.span(name="planning"):
                stack = agent._get_span_stack()
                assert len(stack) == 2
                assert stack[1]["name"] == "planning"
                assert stack[1]["session_id"] == "s1"
            # Span popped
            assert len(agent._get_span_stack()) == 1
        assert len(agent._get_span_stack()) == 0

    def test_span_without_session_is_noop(self):
        agent = _make_agent()
        with agent.span(name="orphan"):
            # No session active, span should not push to stack
            assert len(agent._get_span_stack()) == 0

    def test_current_span_meta_returns_none_without_session(self):
        agent = _make_agent()
        assert agent._current_span_meta() is None

    def test_current_span_meta_returns_dict_with_session(self):
        agent = _make_agent()
        with agent.session(session_id="s1"):
            meta = agent._current_span_meta()
            assert meta is not None
            assert meta["mds_session_id"] == "s1"
            assert "mds_call_id" in meta


# ── Usage recording & aggregation ───────────────────────────────────────────


class TestUsageRecording:
    def test_record_adds_to_buffer(self):
        agent = _make_agent(aggregation_enabled=False)
        agent.record(
            provider="openai", model="gpt-4o",
            input_tokens=100, output_tokens=50,
        )
        assert len(agent._records) == 1
        assert agent._records[0].provider == "openai"
        assert agent._records[0].model == "gpt-4o"

    def test_record_total_tokens_computed(self):
        agent = _make_agent(aggregation_enabled=False)
        agent.record(
            provider="openai", model="gpt-4o",
            input_tokens=100, output_tokens=50,
        )
        assert agent._records[0].total_tokens == 150

    def test_record_buffer_overflow_drops_oldest(self):
        agent = _make_agent(aggregation_enabled=False, max_buffer_size=2)
        agent.record(provider="openai", model="gpt-4o", input_tokens=1, output_tokens=0)
        agent.record(provider="openai", model="gpt-4o", input_tokens=2, output_tokens=0)
        agent.record(provider="openai", model="gpt-4o", input_tokens=3, output_tokens=0)
        assert len(agent._records) == 2
        assert agent._records[0].input_tokens == 2

    def test_aggregate_record_creates_bucket(self):
        agent = _make_agent(aggregation_enabled=True, trace_sample_rate=0.0)
        agent.record(provider="openai", model="gpt-4o", input_tokens=100, output_tokens=50)
        assert len(agent._agg_buckets) == 1
        key = list(agent._agg_buckets.keys())[0]
        assert agent._agg_buckets[key].call_count == 1

    def test_aggregate_violations_kept_as_traces(self):
        agent = _make_agent(aggregation_enabled=True, trace_sample_rate=0.0)
        agent.record(
            provider="openai", model="gpt-4o",
            input_tokens=100, output_tokens=50,
            metadata={"_policy_violation": True},
        )
        assert len(agent._agg_sampled) == 1

    def test_instrumented_providers_empty_initially(self):
        agent = _make_agent()
        assert agent.instrumented_providers() == []


# ── Framework tier detection ────────────────────────────────────────────────


class TestFrameworkTierDetection:
    def test_custom_tier_default(self):
        agent = _make_agent()
        tier = agent._detect_framework_tier()
        assert tier == "custom"

    def test_structured_tier_when_framework_loaded(self):
        import sys
        agent = _make_agent()
        # Fake langgraph module
        sys.modules["langgraph"] = MagicMock()
        try:
            tier = agent._detect_framework_tier()
            assert tier == "structured"
        finally:
            del sys.modules["langgraph"]


# ── Heartbeat & flush (no network, just coverage) ──────────────────────────


class TestHeartbeat:
    def test_send_heartbeat_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        # Should not raise, just return
        agent._send_heartbeat()

    def test_send_heartbeat_with_unreachable_server(self):
        agent = _make_agent()
        # Should not raise, logs debug
        agent._send_heartbeat()


class TestFlush:
    def test_flush_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        agent._flush()  # no-op

    def test_flush_raw_empty_buffer(self):
        agent = _make_agent(aggregation_enabled=False)
        agent._flush_raw()  # no-op

    def test_flush_aggregated_empty(self):
        agent = _make_agent(aggregation_enabled=True)
        agent._flush_aggregated()  # no-op

    def test_shutdown_flush_sets_event(self):
        agent = _make_agent()
        agent._shutdown_flush()
        assert agent._shutdown.is_set()


# ── Cache check/store (response cache) ──────────────────────────────────────


class TestCacheCheckStore:
    def test_cache_check_disabled(self):
        agent = _make_agent(response_cache_enabled=False)
        assert agent._cache_check("openai", "gpt-4o", []) is None

    def test_cache_store_disabled(self):
        agent = _make_agent(response_cache_enabled=False)
        agent._cache_store("openai", "gpt-4o", [], "resp")
        # No error

    def test_cache_check_store_enabled(self):
        agent = _make_agent(response_cache_enabled=True)
        agent._cache_store("openai", "gpt-4o", [{"role": "user"}], "response")
        result = agent._cache_check("openai", "gpt-4o", [{"role": "user"}])
        assert result == "response"


# ── PII scanning (disabled by default) ──────────────────────────────────────


class TestPIIScanning:
    def test_scan_disabled_returns_input(self):
        agent = _make_agent()
        msgs = [{"role": "user", "content": "hello"}]
        result, findings = agent._scan_and_redact_pii(msgs)
        assert result == msgs
        assert findings == []

    def test_apply_pii_scan_disabled(self):
        agent = _make_agent()
        msgs = [{"role": "user", "content": "hello"}]
        result = agent._apply_pii_scan(msgs)
        assert result == msgs


# ── OpenAI provider detection ───────────────────────────────────────────────


class TestOAIProviderDetection:
    def test_detect_xai(self):
        mock_sdk = MagicMock()
        mock_sdk._client._base_url = "https://api.x.ai/v1"
        prov = ModusAgent._detect_oai_provider(mock_sdk)
        assert prov == "xai"

    def test_detect_deepseek(self):
        mock_sdk = MagicMock()
        mock_sdk._client._base_url = "https://api.deepseek.com/v1"
        prov = ModusAgent._detect_oai_provider(mock_sdk)
        assert prov == "deepseek"

    def test_detect_default_openai(self):
        mock_sdk = MagicMock()
        mock_sdk._client._base_url = "https://api.openai.com/v1"
        prov = ModusAgent._detect_oai_provider(mock_sdk)
        assert prov == "openai"

    def test_detect_error_returns_openai(self):
        mock_sdk = MagicMock()
        mock_sdk._client = None  # Will cause AttributeError
        prov = ModusAgent._detect_oai_provider(mock_sdk)
        assert prov == "openai"


# ── Rewind trigger (no hooks = no-op) ──────────────────────────────────────


class TestRewindTrigger:
    def test_trigger_rewind_no_hooks(self):
        agent = _make_agent()
        # Should be a no-op with no registered hooks
        agent._trigger_rewind("test_reason")

    def test_register_and_check(self):
        agent = _make_agent()
        agent.register_rewind_hook("db", lambda ctx: True)
        assert agent._rewind_registry.has_hooks is True
        agent.unregister_rewind_hook("db")
        assert agent._rewind_registry.has_hooks is False
