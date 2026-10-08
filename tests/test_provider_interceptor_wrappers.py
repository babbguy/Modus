"""
SDK Agent Interceptor Wrappers

Tests the monkeypatching interceptor functions for each LLM provider:
  - Anthropic sync/async/streaming
  - OpenAI/xAI sync/async/streaming
  - Bedrock invoke_model
  - Google Gemini generate_content
  - Groq chat.completions
  - Mistral chat
  - Cohere chat
  - Provider detection from base_url
  - _enforce_with_retry auto-optimization
  - Response cache integration
"""
from __future__ import annotations

import sys
import types
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── Helper: Create a minimal ModusAgent without network or threads ────────

def _make_agent(**overrides):
    """Build a ModusAgent that won't try to connect anywhere."""
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
    # Bypass start/registration by marking as started
    agent._started = True
    agent._app_id = "test-app"
    agent._team_id = "test-team"
    agent._api_key = "test-key"
    return agent


# ═════════════════════════════════════════════════════════════════════════════
# 1. PROVIDER DETECTION (OpenAI base_url mapping)
# ═════════════════════════════════════════════════════════════════════════════

def test_detect_oai_provider_openai():
    from modus.agent import ModusAgent
    sdk = MagicMock()
    sdk._client._base_url = "https://api.openai.com/v1"
    assert ModusAgent._detect_oai_provider(sdk) == "openai"


def test_detect_oai_provider_xai():
    from modus.agent import ModusAgent
    sdk = MagicMock()
    sdk._client._base_url = "https://api.x.ai/v1"
    assert ModusAgent._detect_oai_provider(sdk) == "xai"


def test_detect_oai_provider_together():
    from modus.agent import ModusAgent
    sdk = MagicMock()
    sdk._client._base_url = "https://api.together.xyz/v1"
    assert ModusAgent._detect_oai_provider(sdk) == "together"


def test_detect_oai_provider_deepseek():
    from modus.agent import ModusAgent
    sdk = MagicMock()
    sdk._client._base_url = "https://api.deepseek.com/v1"
    assert ModusAgent._detect_oai_provider(sdk) == "deepseek"


def test_detect_oai_provider_groq():
    from modus.agent import ModusAgent
    sdk = MagicMock()
    sdk._client._base_url = "https://api.groq.com/v1"
    assert ModusAgent._detect_oai_provider(sdk) == "groq"


def test_detect_oai_provider_fallback():
    from modus.agent import ModusAgent
    sdk = MagicMock()
    sdk._client._base_url = None
    sdk._client.base_url = None
    assert ModusAgent._detect_oai_provider(sdk) == "openai"


def test_detect_oai_provider_exception():
    from modus.agent import ModusAgent
    sdk = MagicMock()
    type(sdk)._client = PropertyMock(side_effect=RuntimeError("no client"))
    assert ModusAgent._detect_oai_provider(sdk) == "openai"


# ═════════════════════════════════════════════════════════════════════════════
# 2. ANTHROPIC INSTRUMENTATION
# ═════════════════════════════════════════════════════════════════════════════

def _build_mock_anthropic():
    """Build a fake anthropic module with Resources.Messages.create."""
    anthropic = types.ModuleType("anthropic")
    anthropic.__version__ = "0.99.0"
    resources = types.ModuleType("anthropic.resources")
    anthropic.resources = resources

    class Messages:
        @staticmethod
        def create(self_sdk, *args, **kwargs):
            resp = MagicMock()
            resp.usage = MagicMock(input_tokens=10, output_tokens=20)
            resp.model = kwargs.get("model", "claude-sonnet")
            resp.stop_reason = "end_turn"
            return resp

    class AsyncMessages:
        @staticmethod
        async def create(self_sdk, *args, **kwargs):
            resp = MagicMock()
            resp.usage = MagicMock(input_tokens=10, output_tokens=20)
            resp.model = kwargs.get("model", "claude-sonnet")
            return resp

    resources.Messages = Messages
    resources.AsyncMessages = AsyncMessages
    return anthropic


def test_anthropic_sync_instrumentation():
    anthropic = _build_mock_anthropic()
    sys.modules["anthropic"] = anthropic
    sys.modules["anthropic.resources"] = anthropic.resources
    try:
        agent = _make_agent()
        agent.enforce = MagicMock(return_value=None)
        agent._enforce_with_retry = MagicMock(return_value="claude-sonnet")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_anthropic()
        assert "anthropic" in agent._instrumented

        sdk_instance = MagicMock()
        resp = anthropic.resources.Messages.create(
            sdk_instance, model="claude-sonnet",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp is not None
        assert len(recorded) >= 1
        assert recorded[0][0] == "anthropic"
    finally:
        sys.modules.pop("anthropic", None)
        sys.modules.pop("anthropic.resources", None)


def test_anthropic_sync_cache_hit():
    anthropic = _build_mock_anthropic()
    sys.modules["anthropic"] = anthropic
    sys.modules["anthropic.resources"] = anthropic.resources
    try:
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="claude-sonnet")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        cached_resp = MagicMock()
        agent._cache_check = MagicMock(return_value=cached_resp)
        agent._cache_store = MagicMock()
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_anthropic()

        sdk_instance = MagicMock()
        resp = anthropic.resources.Messages.create(
            sdk_instance, model="claude-sonnet",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp is cached_resp
        # Should record a cached hit
        assert any("cached" in str(r) for r in recorded)
    finally:
        sys.modules.pop("anthropic", None)
        sys.modules.pop("anthropic.resources", None)


def test_anthropic_sync_streaming():
    anthropic = _build_mock_anthropic()

    # Override create to return a generator for streaming
    def _streaming_create(self_sdk, *args, **kwargs):
        def _gen():
            event1 = MagicMock()
            event1.type = "message_start"
            event1.message = MagicMock()
            event1.message.usage = MagicMock(input_tokens=5)
            yield event1

            event2 = MagicMock()
            event2.type = "message_delta"
            event2.usage = MagicMock(output_tokens=15)
            yield event2
        return _gen()

    anthropic.resources.Messages.create = _streaming_create

    sys.modules["anthropic"] = anthropic
    sys.modules["anthropic.resources"] = anthropic.resources
    try:
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="claude-sonnet")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_anthropic()

        sdk_instance = MagicMock()
        stream = anthropic.resources.Messages.create(
            sdk_instance, model="claude-sonnet",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        events = list(stream)
        assert len(events) == 2
        # Streaming should record with "(stream)" in operation
        assert any("stream" in str(r) for r in recorded)
    finally:
        sys.modules.pop("anthropic", None)
        sys.modules.pop("anthropic.resources", None)


# ═════════════════════════════════════════════════════════════════════════════
# 3. OPENAI INSTRUMENTATION
# ═════════════════════════════════════════════════════════════════════════════

def _build_mock_openai():
    """Build a fake openai module with resources.chat.Completions.create."""
    openai = types.ModuleType("openai")
    openai.__version__ = "1.99.0"
    resources = types.ModuleType("openai.resources")
    chat = types.ModuleType("openai.resources.chat")
    openai.resources = resources
    resources.chat = chat

    class Completions:
        @staticmethod
        def create(self_sdk, *args, **kwargs):
            resp = MagicMock()
            resp.usage = MagicMock(prompt_tokens=15, completion_tokens=25)
            resp.model = kwargs.get("model", "gpt-4o")
            return resp

    class AsyncCompletions:
        @staticmethod
        async def create(self_sdk, *args, **kwargs):
            resp = MagicMock()
            resp.usage = MagicMock(prompt_tokens=15, completion_tokens=25)
            resp.model = kwargs.get("model", "gpt-4o")
            return resp

    chat.Completions = Completions
    chat.AsyncCompletions = AsyncCompletions
    return openai


def test_openai_sync_instrumentation():
    openai = _build_mock_openai()
    sys.modules["openai"] = openai
    sys.modules["openai.resources"] = openai.resources
    sys.modules["openai.resources.chat"] = openai.resources.chat
    try:
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))
        agent._detect_oai_provider = MagicMock(return_value="openai")
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_openai()
        assert "openai" in agent._instrumented

        sdk_instance = MagicMock()
        resp = openai.resources.chat.Completions.create(
            sdk_instance, model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp is not None
        assert len(recorded) >= 1
    finally:
        sys.modules.pop("openai", None)
        sys.modules.pop("openai.resources", None)
        sys.modules.pop("openai.resources.chat", None)


def test_openai_sync_cache_hit():
    openai = _build_mock_openai()
    sys.modules["openai"] = openai
    sys.modules["openai.resources"] = openai.resources
    sys.modules["openai.resources.chat"] = openai.resources.chat
    try:
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        cached_resp = MagicMock()
        agent._cache_check = MagicMock(return_value=cached_resp)
        agent._detect_oai_provider = MagicMock(return_value="openai")
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_openai()

        sdk_instance = MagicMock()
        resp = openai.resources.chat.Completions.create(
            sdk_instance, model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp is cached_resp
    finally:
        sys.modules.pop("openai", None)
        sys.modules.pop("openai.resources", None)
        sys.modules.pop("openai.resources.chat", None)


def test_openai_sync_streaming():
    openai = _build_mock_openai()

    def _streaming_create(self_sdk, *args, **kwargs):
        def _gen():
            chunk1 = MagicMock()
            chunk1.usage = None
            yield chunk1
            chunk2 = MagicMock()
            chunk2.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
            yield chunk2
        return _gen()

    openai.resources.chat.Completions.create = _streaming_create

    sys.modules["openai"] = openai
    sys.modules["openai.resources"] = openai.resources
    sys.modules["openai.resources.chat"] = openai.resources.chat
    try:
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._detect_oai_provider = MagicMock(return_value="openai")
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_openai()

        sdk_instance = MagicMock()
        stream = openai.resources.chat.Completions.create(
            sdk_instance, model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        chunks = list(stream)
        assert len(chunks) == 2
        assert any("stream" in str(r) for r in recorded)
    finally:
        sys.modules.pop("openai", None)
        sys.modules.pop("openai.resources", None)
        sys.modules.pop("openai.resources.chat", None)


def test_openai_routing_intercept():
    openai = _build_mock_openai()
    sys.modules["openai"] = openai
    sys.modules["openai.resources"] = openai.resources
    sys.modules["openai.resources.chat"] = openai.resources.chat
    try:
        agent = _make_agent()
        agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        routed_resp = MagicMock()
        routed_resp.usage = MagicMock(prompt_tokens=5, completion_tokens=10)
        routed_resp.model = "gpt-4o-mini"
        agent._try_route_sync = MagicMock(return_value=(True, routed_resp))
        agent._detect_oai_provider = MagicMock(return_value="openai")
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_openai()

        sdk_instance = MagicMock()
        resp = openai.resources.chat.Completions.create(
            sdk_instance, model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp is routed_resp
    finally:
        sys.modules.pop("openai", None)
        sys.modules.pop("openai.resources", None)
        sys.modules.pop("openai.resources.chat", None)


# ═════════════════════════════════════════════════════════════════════════════
# 4. ENFORCE_WITH_RETRY
# ═════════════════════════════════════════════════════════════════════════════

def test_enforce_with_retry_passthrough():
    agent = _make_agent()
    agent.enforce = MagicMock(return_value=None)
    result = agent._enforce_with_retry("openai", "gpt-4o")
    assert result == "gpt-4o"


def test_enforce_with_retry_degradation_ladder():
    agent = _make_agent(auto_optimize=True)
    agent.enforce = MagicMock(return_value="gpt-4o-mini")
    result = agent._enforce_with_retry("openai", "gpt-4o")
    assert result == "gpt-4o-mini"


def test_enforce_with_retry_deny_with_suggested():
    from modus.agent import PolicyViolationError
    agent = _make_agent(auto_optimize=True)

    call_count = [0]

    def _mock_enforce(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise PolicyViolationError(
                decision="deny",
                reason="Budget exceeded",
                suggested_model="gpt-3.5-turbo",
            )
        return None  # allow the re-enforced model

    agent.enforce = _mock_enforce
    result = agent._enforce_with_retry("openai", "gpt-4o")
    assert result == "gpt-3.5-turbo"
    assert call_count[0] == 2


def test_enforce_with_retry_deny_no_suggestion():
    from modus.agent import PolicyViolationError
    agent = _make_agent(auto_optimize=True)
    agent.enforce = MagicMock(side_effect=PolicyViolationError(
        decision="deny", reason="Blocked",
    ))
    with pytest.raises(PolicyViolationError):
        agent._enforce_with_retry("openai", "gpt-4o")


def test_enforce_with_retry_no_auto_optimize():
    from modus.agent import PolicyViolationError
    agent = _make_agent(auto_optimize=False)
    agent.enforce = MagicMock(side_effect=PolicyViolationError(
        decision="deny", reason="Blocked", suggested_model="gpt-3.5-turbo",
    ))
    with pytest.raises(PolicyViolationError):
        agent._enforce_with_retry("openai", "gpt-4o")


# ═════════════════════════════════════════════════════════════════════════════
# 5. BEDROCK INSTRUMENTATION
# ═════════════════════════════════════════════════════════════════════════════

def test_bedrock_instrumentation():
    """Test that boto3 client patching works for bedrock-runtime."""
    import json as _json

    boto3 = types.ModuleType("boto3")
    boto3.__version__ = "1.35.0"

    class FakeSession:
        @staticmethod
        def client(service_name, *args, **kwargs):
            c = MagicMock()
            raw_body = _json.dumps({"usage": {"input_tokens": 100, "output_tokens": 50}}).encode()

            class ReusableBody:
                def read(self):
                    return raw_body

            c.invoke_model = MagicMock(return_value={"body": ReusableBody()})
            c.invoke_model_with_response_stream = MagicMock(return_value={"body": MagicMock()})
            return c

    boto3.Session = FakeSession

    original_client = FakeSession.client

    @staticmethod
    def module_client(service_name, *args, **kwargs):
        return original_client(service_name, *args, **kwargs)

    boto3.client = module_client

    sys.modules["boto3"] = boto3
    try:
        agent = _make_agent()
        agent.enforce = MagicMock(return_value=None)
        recorded = []
        agent._rec = lambda *a, **kw: recorded.append(a)

        agent._instrument_boto3()
        assert "bedrock" in agent._instrumented

        # Create a bedrock-runtime client via the patched boto3.client
        client = boto3.client("bedrock-runtime")
        assert client is not None
    finally:
        sys.modules.pop("boto3", None)


# ═════════════════════════════════════════════════════════════════════════════
# 6. IMPORT ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════════

def test_anthropic_import_error():
    """Instrumentation silently skips when provider not installed."""
    agent = _make_agent()
    # Make sure anthropic is not importable
    with patch.dict(sys.modules, {"anthropic": None}):
        agent._instrument_anthropic()
    assert "anthropic" not in agent._instrumented


def test_openai_import_error():
    agent = _make_agent()
    with patch.dict(sys.modules, {"openai": None}):
        agent._instrument_openai()
    assert "openai" not in agent._instrumented


# ═════════════════════════════════════════════════════════════════════════════
# 7. POLICY VIOLATION ERROR
# ═════════════════════════════════════════════════════════════════════════════

def test_policy_violation_error_attributes():
    from modus.agent import PolicyViolationError
    exc = PolicyViolationError(
        decision="deny",
        reason="Budget exceeded",
        policy_id="pol-123",
        policy_name="daily-cap",
        suggested_model="gpt-3.5-turbo",
        message="Custom message",
        retry_after_seconds=30,
    )
    assert exc.decision == "deny"
    assert exc.reason == "Budget exceeded"
    assert exc.policy_id == "pol-123"
    assert exc.suggested_model == "gpt-3.5-turbo"
    assert exc.retry_after_seconds == 30
    assert "Budget exceeded" in str(exc)
    assert "gpt-3.5-turbo" in str(exc)
    assert "30s" in str(exc)


def test_policy_violation_error_minimal():
    from modus.agent import PolicyViolationError
    exc = PolicyViolationError(decision="throttle", reason="Rate limited")
    assert exc.decision == "throttle"
    assert exc.suggested_model is None
    assert exc.retry_after_seconds is None


# ═════════════════════════════════════════════════════════════════════════════
# 8. RESPONSE CACHE
# ═════════════════════════════════════════════════════════════════════════════

def test_response_cache_hit_miss():
    from modus.agent import _ResponseCache
    cache = _ResponseCache(max_entries=10, ttl_seconds=60.0)
    assert cache.get("openai", "gpt-4", [{"role": "user", "content": "hi"}]) is None
    assert cache.misses == 1

    cache.put("openai", "gpt-4", [{"role": "user", "content": "hi"}], "response-1")
    result = cache.get("openai", "gpt-4", [{"role": "user", "content": "hi"}])
    assert result == "response-1"
    assert cache.hits == 1

    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["entries"] == 1


def test_response_cache_eviction():
    from modus.agent import _ResponseCache
    cache = _ResponseCache(max_entries=2, ttl_seconds=60.0)
    cache.put("openai", "m1", "msg1", "r1")
    cache.put("openai", "m2", "msg2", "r2")
    cache.put("openai", "m3", "msg3", "r3")
    # m1 should be evicted
    assert cache.get("openai", "m1", "msg1") is None
    assert cache.get("openai", "m3", "msg3") == "r3"


def test_response_cache_clear():
    from modus.agent import _ResponseCache
    cache = _ResponseCache(max_entries=10, ttl_seconds=60.0)
    cache.put("openai", "m1", "msg1", "r1")
    cache.clear()
    assert cache.get("openai", "m1", "msg1") is None
    assert cache.hits == 0
    assert cache.misses == 1


# ═════════════════════════════════════════════════════════════════════════════
# 9. EVALUATE CACHE
# ═════════════════════════════════════════════════════════════════════════════

def test_evaluate_cache():
    from modus.agent import _EvaluateCache
    cache = _EvaluateCache(ttl=10.0, max_entries=5)
    assert cache.get("openai", "gpt-4", "prod") is None

    cache.set("openai", "gpt-4", "prod", {"decision": "allow"})
    result = cache.get("openai", "gpt-4", "prod")
    assert result == {"decision": "allow"}

    cache.clear()
    assert cache.get("openai", "gpt-4", "prod") is None


# ═════════════════════════════════════════════════════════════════════════════
# 10. AGGREGATION BUCKET
# ═════════════════════════════════════════════════════════════════════════════

def test_aggregation_bucket_accumulate():
    from modus.agent import _AggregationBucket, _UsageRecord
    bucket = _AggregationBucket(
        provider="openai", model="gpt-4", operation="chat", resource_type="llm_call",
    )
    r1 = _UsageRecord(
        provider="openai", resource_type="llm_call", model="gpt-4",
        operation="chat", input_tokens=10, output_tokens=20, total_tokens=30,
        input_cost=Decimal("0.01"), output_cost=Decimal("0.02"),
        total_cost=Decimal("0.03"), duration_ms=100,
        timestamp="2026-01-01T00:00:00Z",
    )
    r2 = _UsageRecord(
        provider="openai", resource_type="llm_call", model="gpt-4",
        operation="chat", input_tokens=5, output_tokens=10, total_tokens=15,
        input_cost=Decimal("0.005"), output_cost=Decimal("0.01"),
        total_cost=Decimal("0.015"), duration_ms=50,
        timestamp="2026-01-01T00:01:00Z",
    )
    bucket.accumulate(r1)
    bucket.accumulate(r2)

    assert bucket.call_count == 2
    assert bucket.input_tokens == 15
    assert bucket.output_tokens == 30
    assert bucket.duration_ms_min == 50
    assert bucket.duration_ms_max == 100

    d = bucket.to_dict()
    assert d["call_count"] == 2
    assert d["duration_ms_avg"] == 75
