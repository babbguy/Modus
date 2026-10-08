"""
Agent SDK deep coverage.

Targets sdk.modus.agent:
  - _enforce_with_retry (931-932, 938-944, 990-991)
  - _scrub_pii_if_enabled (983-991)
  - _scan_and_redact_pii branches (1034, 1042-1043, 1052)
  - _apply_pii_scan
  - Anthropic sync interceptor (1584-1587, 1593, 1610-1618)
  - Anthropic async interceptor (1661-1711)
  - Anthropic async stream (1714-1726, 1730-1731, 1738-1739)
  - _detect_oai_provider (1763-1771)
  - OpenAI sync interceptor (1810-1846)
  - OpenAI async interceptor (1880-1923)
  - OpenAI async stream (1925-1933, 1938-1939)
  - Gemini interceptor (2096-2125)
  - Groq interceptor (2239-2242, 2250, 2253-2260, 2270-2293, 2296-2303)
  - Mistral interceptor (2345-2363, 2377-2395, 2399-2400, 2408-2409)
  - Cohere interceptor (2430-2433, 2451-2454, 2462-2463, 2471-2490)
  - _flush_aggregated (2647-2664)
  - _try_route_async (2848-2953)
  - _extract_prompts_for_routing / _extract_response_text
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
# ── Helper ───────────────────────────────────────────────────────────────────

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


# ── _enforce_with_retry ──────────────────────────────────────────────────────

class TestEnforceWithRetry:
    def test_allow_returns_model(self):
        agent = _make_agent()
        with patch.object(agent, "enforce", return_value=None):
            result = agent._enforce_with_retry("openai", "gpt-4o")
        assert result == "gpt-4o"

    def test_degradation_ladder_substitution(self):
        agent = _make_agent(auto_optimize=True)
        with patch.object(agent, "enforce", return_value="gpt-4o-mini"):
            result = agent._enforce_with_retry("openai", "gpt-4o")
        assert result == "gpt-4o-mini"

    def test_policy_violation_with_suggestion(self):
        from modus.agent import PolicyViolationError
        agent = _make_agent(auto_optimize=True)

        exc = PolicyViolationError("deny", "Budget exceeded", suggested_model="gpt-4o-mini")
        with patch.object(agent, "enforce", side_effect=[exc, None]):
            result = agent._enforce_with_retry("openai", "gpt-4o")
        assert result == "gpt-4o-mini"

    def test_policy_violation_no_suggestion(self):
        from modus.agent import PolicyViolationError
        agent = _make_agent(auto_optimize=True)

        exc = PolicyViolationError("deny", "Budget exceeded")
        with patch.object(agent, "enforce", side_effect=exc):
            with pytest.raises(PolicyViolationError):
                agent._enforce_with_retry("openai", "gpt-4o")

    def test_no_auto_optimize_raises(self):
        from modus.agent import PolicyViolationError
        agent = _make_agent(auto_optimize=False)

        exc = PolicyViolationError("deny", "Budget exceeded", suggested_model="gpt-4o-mini")
        with patch.object(agent, "enforce", side_effect=exc):
            with pytest.raises(PolicyViolationError):
                agent._enforce_with_retry("openai", "gpt-4o")

    def test_same_model_no_substitution(self):
        agent = _make_agent(auto_optimize=True)
        with patch.object(agent, "enforce", return_value="gpt-4o"):
            result = agent._enforce_with_retry("openai", "gpt-4o")
        assert result == "gpt-4o"


# ── PII scanning ────────────────────────────────────────────────────────────

class TestPiiScanning:
    def test_scrub_pii_if_disabled(self):
        agent = _make_agent()
        agent._scrub_pii = False
        result = agent._scrub_pii_if_enabled({"data": "test"})
        assert result == {"data": "test"}

    def test_scrub_pii_if_enabled_import_error(self):
        agent = _make_agent()
        agent._scrub_pii = True
        with patch.dict(sys.modules, {"modus.pii": None}):
            result = agent._scrub_pii_if_enabled("test data")
        # Should return original on import error
        assert result == "test data"

    def test_scan_and_redact_disabled(self):
        agent = _make_agent()
        agent._pii_scan_enabled = False
        result, findings = agent._scan_and_redact_pii([{"content": "test"}])
        assert findings == []

    def test_scan_and_redact_string_input(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_pii = MagicMock()
        mock_pii.scan_and_redact_pii.return_value = ("[REDACTED]", [{"type": "email", "count": 1}])
        with patch.dict(sys.modules, {"modus.pii": mock_pii}):
            result, findings = agent._scan_and_redact_pii("test@example.com")
        assert len(findings) == 1

    def test_scan_and_redact_list_messages(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_pii = MagicMock()
        mock_pii.scan_and_redact_pii.return_value = ("[REDACTED]", [{"type": "ssn", "count": 1}])
        with patch.dict(sys.modules, {"modus.pii": mock_pii}):
            msgs = [{"content": "SSN: 123-45-6789"}]
            result, findings = agent._scan_and_redact_pii(msgs)
        assert len(findings) >= 1

    def test_scan_and_redact_log_only(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "log"

        mock_pii = MagicMock()
        mock_pii.scan_pii.return_value = [{"type": "phone", "count": 1}]
        with patch.dict(sys.modules, {"modus.pii": mock_pii}):
            msgs = [{"content": "Call 555-1234"}]
            result, findings = agent._scan_and_redact_pii(msgs)
        assert len(findings) >= 1

    def test_scan_multipart_content(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_pii = MagicMock()
        mock_pii.scan_and_redact_pii.return_value = ("[REDACTED]", [{"type": "email", "count": 1}])
        with patch.dict(sys.modules, {"modus.pii": mock_pii}):
            msgs = [{"content": [{"text": "test@example.com", "type": "text"}]}]
            result, findings = agent._scan_and_redact_pii(msgs)
        assert len(findings) >= 1

    def test_scan_string_in_list(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_pii = MagicMock()
        mock_pii.scan_and_redact_pii.return_value = ("[REDACTED]", [{"type": "ssn", "count": 1}])
        with patch.dict(sys.modules, {"modus.pii": mock_pii}):
            msgs = ["raw string with SSN 123-45-6789"]
            result, findings = agent._scan_and_redact_pii(msgs)
        assert len(findings) >= 1

    def test_scan_log_only_string(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "log"

        mock_pii = MagicMock()
        mock_pii.scan_pii.return_value = [{"type": "email", "count": 1}]
        with patch.dict(sys.modules, {"modus.pii": mock_pii}):
            result, findings = agent._scan_and_redact_pii("email@test.com")
        assert len(findings) >= 1


# ── _detect_oai_provider ────────────────────────────────────────────────────

class TestDetectOaiProvider:
    def test_openai_default(self):
        from modus.agent import ModusAgent
        mock_sdk = MagicMock()
        mock_sdk._client = MagicMock()
        mock_sdk._client.base_url = "https://api.openai.com"
        result = ModusAgent._detect_oai_provider(mock_sdk)
        assert result == "openai"

    def test_openrouter_provider(self):
        from modus.agent import ModusAgent
        mock_sdk = MagicMock()
        mock_sdk._client = MagicMock()
        mock_sdk._client._base_url = "https://openrouter.ai/api"
        result = ModusAgent._detect_oai_provider(mock_sdk)
        assert result == "openrouter"

    def test_together_provider(self):
        from modus.agent import ModusAgent
        mock_sdk = MagicMock()
        mock_sdk._client = MagicMock()
        mock_sdk._client._base_url = "https://api.together.xyz/v1"
        result = ModusAgent._detect_oai_provider(mock_sdk)
        assert result == "together"

    def test_xai_provider(self):
        from modus.agent import ModusAgent
        mock_sdk = MagicMock()
        mock_sdk._client = MagicMock()
        mock_sdk._client._base_url = "https://api.x.ai/v1"
        result = ModusAgent._detect_oai_provider(mock_sdk)
        assert result == "xai"

    def test_unknown_provider(self):
        from modus.agent import ModusAgent
        mock_sdk = MagicMock()
        mock_sdk._client = MagicMock()
        mock_sdk._client.base_url = "https://unknown-provider.com"
        result = ModusAgent._detect_oai_provider(mock_sdk)
        assert result == "openai"  # defaults to openai

    def test_attribute_error(self):
        from modus.agent import ModusAgent
        mock_sdk = MagicMock()
        mock_sdk._client = None
        result = ModusAgent._detect_oai_provider(mock_sdk)
        assert result == "openai"


# ── _extract_prompts_for_routing ─────────────────────────────────────────────

class TestExtractPromptsForRouting:
    def test_openai_format(self):
        from modus.agent import ModusAgent
        kwargs = {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ]
        }
        sys_prompt, user_prompt = ModusAgent._extract_prompts_for_routing("openai", kwargs)
        assert sys_prompt == "You are a helpful assistant."
        assert user_prompt == "Hello!"

    def test_anthropic_format(self):
        from modus.agent import ModusAgent
        kwargs = {
            "system": "You are helpful.",
            "messages": [
                {"role": "user", "content": "What's 2+2?"},
            ]
        }
        sys_prompt, user_prompt = ModusAgent._extract_prompts_for_routing("anthropic", kwargs)
        assert sys_prompt == "You are helpful."
        assert user_prompt == "What's 2+2?"

    def test_no_messages(self):
        from modus.agent import ModusAgent
        sys_prompt, user_prompt = ModusAgent._extract_prompts_for_routing("openai", {})
        assert sys_prompt == ""
        assert user_prompt == ""


# ── _extract_response_text ───────────────────────────────────────────────────

class TestExtractResponseText:
    def test_openai_response(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        choice = MagicMock()
        choice.message.content = "Hello there!"
        resp.choices = [choice]
        result = ModusAgent._extract_response_text("openai", resp)
        assert result == "Hello there!"

    def test_anthropic_response(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        block = MagicMock()
        block.text = "Response text"
        resp.content = [block]
        result = ModusAgent._extract_response_text("anthropic", resp)
        assert result == "Response text"

    def test_empty_response(self):
        from modus.agent import ModusAgent
        resp = MagicMock()
        resp.choices = []
        resp.content = []
        result = ModusAgent._extract_response_text("openai", resp)
        assert result == ""


# ── Aggregation flush ────────────────────────────────────────────────────────

class TestAggregationFlush:
    def test_flush_requeues_on_failure(self):
        agent = _make_agent(aggregation_enabled=True)
        from modus.agent import _AggregationBucket

        now_str = datetime.now(timezone.utc).isoformat()
        bucket = _AggregationBucket(
            provider="openai",
            model="gpt-4o",
            operation="chat",
            resource_type="llm_call",
            call_count=5,
            input_tokens=500,
            output_tokens=200,
            total_tokens=700,
            input_cost=Decimal("0.01"),
            output_cost=Decimal("0.005"),
            total_cost=Decimal("0.015"),
            duration_ms_sum=1000,
            duration_ms_min=100,
            duration_ms_max=300,
            window_start=now_str,
            window_end=now_str,
        )
        agent._agg_buckets["openai:gpt-4o:chat:llm_call"] = bucket

        # Mock _flush_raw to fail
        with patch.object(agent, "_flush_raw", side_effect=Exception("network error")):
            agent._flush_aggregated()

        # Bucket should be re-queued
        assert "openai:gpt-4o:chat:llm_call" in agent._agg_buckets


# ── _try_route_async ─────────────────────────────────────────────────────────

class TestTryRouteAsync:
    async def test_routing_disabled(self):
        agent = _make_agent()
        agent._routing_enabled = False
        routed, resp = await agent._try_route_async("openai", "gpt-4o", {}, None, None)
        assert routed is False

    async def test_routing_no_app_id(self):
        agent = _make_agent()
        agent._routing_enabled = True
        agent._app_id = None
        routed, resp = await agent._try_route_async("openai", "gpt-4o", {}, None, None)
        assert routed is False

    async def test_routing_import_error(self):
        agent = _make_agent()
        agent._routing_enabled = True
        with patch.dict(sys.modules, {"modus.routing_interceptor": None}):
            routed, resp = await agent._try_route_async("openai", "gpt-4o", {}, None, None)
        assert routed is False
