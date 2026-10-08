"""
Tests for sdk/modus/agent.py — interceptor wrapping, PII scanning,
heartbeat construction, response cache integration, circuit breaker,
and gateway health checking.

Covers the line ranges: 586-944, 987-1083, 1147-1226, 1400-2953.
"""
from __future__ import annotations

import json
import time
import types
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from modus.agent import (
    ModusAgent,
    PolicyViolationError,
    __version__,
)


# ── Helper ────────────────────────────────────────────────────────────────────

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
    agent._api_key = "mds_test_key"
    agent._app_id = "test-app"
    agent._team_id = "team-1"
    return agent


def _mock_usage(input_tokens=10, output_tokens=5):
    """Create a mock object with usage attributes."""
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.prompt_tokens = input_tokens
    u.completion_tokens = output_tokens
    return u


def _mock_response(model="gpt-4o", input_tokens=10, output_tokens=5, stop_reason="end_turn"):
    resp = MagicMock()
    resp.model = model
    resp.stop_reason = stop_reason
    resp.usage = _mock_usage(input_tokens, output_tokens)
    return resp


# ── PII Scanning ──────────────────────────────────────────────────────────────


class TestPIIScanningEnabled:
    """Tests for _scan_and_redact_pii and _apply_pii_scan with PII enabled."""

    def test_scan_and_redact_with_string_messages(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_scan_and_redact = MagicMock(return_value=("redacted text", [{"type": "email", "count": 1}]))
        mock_scan = MagicMock()

        with patch.dict("sys.modules", {}):
            with patch("modus.pii.scan_and_redact_pii", mock_scan_and_redact, create=True):
                with patch("modus.pii.scan_pii", mock_scan, create=True):
                    result, findings = agent._scan_and_redact_pii("user@example.com")

        assert findings == [{"type": "email", "count": 1}]

    def test_scan_and_redact_with_list_messages_redact_action(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_scan_and_redact = MagicMock(return_value=("[REDACTED]", [{"type": "email", "count": 1}]))

        pii_module = types.ModuleType("modus.pii")
        pii_module.scan_and_redact_pii = mock_scan_and_redact
        pii_module.scan_pii = MagicMock()

        with patch.dict("sys.modules", {"modus.pii": pii_module}):
            msgs = [{"role": "user", "content": "user@example.com"}]
            result, findings = agent._scan_and_redact_pii(msgs)

        assert len(findings) == 1
        assert result[0]["content"] == "[REDACTED]"

    def test_scan_and_redact_log_only_action(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "log"

        mock_scan_pii = MagicMock(return_value=[{"type": "ssn", "count": 1}])

        pii_module = types.ModuleType("modus.pii")
        pii_module.scan_pii = mock_scan_pii
        pii_module.scan_and_redact_pii = MagicMock()

        with patch.dict("sys.modules", {"modus.pii": pii_module}):
            msgs = [{"role": "user", "content": "123-45-6789"}]
            result, findings = agent._scan_and_redact_pii(msgs)

        assert len(findings) == 1
        # Content should NOT be modified in log-only mode
        assert result[0]["content"] == "123-45-6789"

    def test_scan_and_redact_multipart_content(self):
        """Test PII scanning of Anthropic-style multi-part content blocks."""
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_scan_and_redact = MagicMock(return_value=("[REDACTED]", [{"type": "phone", "count": 1}]))

        pii_module = types.ModuleType("modus.pii")
        pii_module.scan_and_redact_pii = mock_scan_and_redact
        pii_module.scan_pii = MagicMock()

        with patch.dict("sys.modules", {"modus.pii": pii_module}):
            msgs = [{"role": "user", "content": [{"type": "text", "text": "Call 555-1234"}]}]
            result, findings = agent._scan_and_redact_pii(msgs)

        assert len(findings) == 1

    def test_scan_and_redact_string_in_list(self):
        """Test PII scanning of plain strings in a list (Google GenAI style)."""
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_scan_and_redact = MagicMock(return_value=("[REDACTED]", [{"type": "email", "count": 1}]))

        pii_module = types.ModuleType("modus.pii")
        pii_module.scan_and_redact_pii = mock_scan_and_redact
        pii_module.scan_pii = MagicMock()

        with patch.dict("sys.modules", {"modus.pii": pii_module}):
            msgs = ["user@example.com"]
            result, findings = agent._scan_and_redact_pii(msgs)

        assert len(findings) == 1

    def test_apply_pii_scan_block_action_raises(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "block"

        mock_scan_and_redact = MagicMock(return_value=("[REDACTED]", [{"type": "email", "count": 1}]))

        pii_module = types.ModuleType("modus.pii")
        pii_module.scan_and_redact_pii = mock_scan_and_redact
        pii_module.scan_pii = MagicMock()

        with patch.dict("sys.modules", {"modus.pii": pii_module}):
            with pytest.raises(PolicyViolationError, match="PII detected"):
                agent._apply_pii_scan([{"role": "user", "content": "user@test.com"}])

    def test_apply_pii_scan_redact_action_returns_redacted(self):
        agent = _make_agent()
        agent._pii_scan_enabled = True
        agent._pii_action = "redact"

        mock_scan_and_redact = MagicMock(return_value=("[REDACTED]", [{"type": "email", "count": 1}]))

        pii_module = types.ModuleType("modus.pii")
        pii_module.scan_and_redact_pii = mock_scan_and_redact
        pii_module.scan_pii = MagicMock()

        with patch.dict("sys.modules", {"modus.pii": pii_module}):
            result = agent._apply_pii_scan([{"role": "user", "content": "user@test.com"}])

        assert result[0]["content"] == "[REDACTED]"

    def test_apply_pii_scan_disabled_uses_legacy_scrub(self):
        agent = _make_agent(scrub_pii=True)
        agent._pii_scan_enabled = False

        pii_module = types.ModuleType("modus.pii")
        pii_module.scrub_pii_recursive = MagicMock(return_value="scrubbed")

        with patch.dict("sys.modules", {"modus.pii": pii_module}):
            result = agent._apply_pii_scan("sensitive data")

        assert result == "scrubbed"

    def test_scan_import_error_returns_input(self):
        """If modus.pii can't be imported, return input unchanged."""
        agent = _make_agent()
        agent._pii_scan_enabled = True

        # Remove pii module if it exists
        with patch.dict("sys.modules", {"modus.pii": None}):
            result, findings = agent._scan_and_redact_pii("test data")

        assert result == "test data"
        assert findings == []


# ── Heartbeat Payload ─────────────────────────────────────────────────────────


class TestHeartbeatPayload:
    def test_heartbeat_payload_structure(self):
        """Verify the heartbeat sends the correct payload shape."""
        agent = _make_agent()
        agent._instrumented = ["anthropic", "openai"]
        agent._sdk_versions = {"anthropic": "0.30.0", "openai": "1.0.0"}

        captured_payload = {}

        def fake_urlopen(req, timeout=None, context=None):
            body = json.loads(req.data.decode())
            captured_payload.update(body)
            resp = MagicMock()
            resp.read.return_value = b'{"ok": true}'
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            agent._send_heartbeat()

        assert captured_payload["agent_version"] == __version__
        assert captured_payload["instrumented_providers"] == ["anthropic", "openai"]
        assert "python" in captured_payload["host_info"]
        assert "pid" in captured_payload["host_info"]

    def test_heartbeat_handles_response_with_routing(self):
        """Verify heartbeat response processing calls update_routing_table."""
        agent = _make_agent()

        resp_body = json.dumps({
            "ok": True,
            "routing_fingerprints": [{"hash": "abc", "cheap_model": "gpt-4o-mini"}],
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body

        with patch("modus.routing_interceptor.update_routing_table") as mock_update:
            agent._handle_heartbeat_response(mock_resp)
            mock_update.assert_called_once_with(
                [{"hash": "abc", "cheap_model": "gpt-4o-mini"}]
            )

    def test_heartbeat_handles_response_without_routing(self):
        """Heartbeat response with no routing data is a no-op."""
        agent = _make_agent()
        resp_body = json.dumps({"ok": True}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        # Should not raise
        agent._handle_heartbeat_response(mock_resp)

    def test_heartbeat_response_parse_error(self):
        """Malformed heartbeat response should not raise."""
        agent = _make_agent()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        # Should not raise
        agent._handle_heartbeat_response(mock_resp)


# ── _call_evaluate (Gateway) ─────────────────────────────────────────────────


class TestCallEvaluate:
    def test_successful_evaluate(self):
        agent = _make_agent()

        resp_body = json.dumps({"decision": "allow"}).encode()

        def fake_urlopen(req, timeout=None, context=None):
            resp = MagicMock()
            resp.read.return_value = resp_body
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "allow"
        assert agent._gateway_consecutive_failures == 0

    def test_evaluate_http_500_fail_open(self):
        from urllib.error import HTTPError

        agent = _make_agent(fail_open=True)

        def fake_urlopen(req, timeout=None, context=None):
            raise HTTPError(req.full_url, 500, "Internal Error", {}, BytesIO(b"{}"))

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "allow"
        assert agent._gateway_consecutive_failures == 1

    def test_evaluate_http_500_fail_closed(self):
        from urllib.error import HTTPError

        agent = _make_agent(fail_open=False)

        def fake_urlopen(req, timeout=None, context=None):
            raise HTTPError(req.full_url, 500, "Internal Error", {}, BytesIO(b"{}"))

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "deny"

    def test_evaluate_url_error_fail_open(self):
        from urllib.error import URLError

        agent = _make_agent(fail_open=True)

        def fake_urlopen(req, timeout=None, context=None):
            raise URLError("Connection refused")

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "allow"
        assert "unreachable" in result["reason"]

    def test_evaluate_url_error_fail_closed(self):
        from urllib.error import URLError

        agent = _make_agent(fail_open=False)

        def fake_urlopen(req, timeout=None, context=None):
            raise URLError("Connection refused")

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "deny"

    def test_evaluate_generic_exception_trips_circuit_breaker(self):
        agent = _make_agent(fail_open=True)
        agent._circuit_breaker_threshold = 1  # trip immediately

        def fake_urlopen(req, timeout=None, context=None):
            raise RuntimeError("Something broke")

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "allow"
        assert agent._circuit_breaker_tripped_at is not None


# ── Circuit Breaker (detailed) ───────────────────────────────────────────────


class TestCircuitBreakerDetailed:
    def test_circuit_breaker_open_blocks_gateway_call(self):
        """When circuit breaker is open, no network call is made."""
        agent = _make_agent(fail_open=True)
        agent._circuit_breaker_tripped_at = time.monotonic()
        agent._circuit_breaker_cooldown = 60.0

        # If a network call were attempted, this would raise
        with patch("modus.agent._urlopen_tls", side_effect=AssertionError("Should not call")):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "allow"
        assert "Circuit breaker" in result["reason"]

    def test_circuit_breaker_half_open_retries(self):
        """After cooldown, circuit breaker enters half-open and retries."""
        agent = _make_agent(fail_open=True)
        agent._circuit_breaker_tripped_at = time.monotonic() - 120
        agent._circuit_breaker_cooldown = 60.0

        resp_body = json.dumps({"decision": "allow"}).encode()

        def fake_urlopen(req, timeout=None, context=None):
            resp = MagicMock()
            resp.read.return_value = resp_body
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._call_evaluate("openai", "gpt-4o", None, None, "llm_call")

        assert result["decision"] == "allow"
        assert agent._circuit_breaker_tripped_at is None  # reset after success

    def test_maybe_trip_circuit_breaker(self):
        agent = _make_agent()
        agent._circuit_breaker_threshold = 3
        agent._gateway_consecutive_failures = 2
        agent._maybe_trip_circuit_breaker()
        assert agent._circuit_breaker_tripped_at is None  # not yet

        agent._gateway_consecutive_failures = 3
        agent._maybe_trip_circuit_breaker()
        assert agent._circuit_breaker_tripped_at is not None

    def test_circuit_breaker_does_not_trip_twice(self):
        agent = _make_agent()
        agent._circuit_breaker_threshold = 1
        agent._gateway_consecutive_failures = 1
        agent._maybe_trip_circuit_breaker()
        first_trip = agent._circuit_breaker_tripped_at
        agent._gateway_consecutive_failures = 10
        agent._maybe_trip_circuit_breaker()
        assert agent._circuit_breaker_tripped_at == first_trip


# ── Gateway Health ────────────────────────────────────────────────────────────


class TestGatewayHealthDetailed:
    def test_gateway_status_includes_all_fields(self):
        agent = _make_agent()
        agent._gateway_consecutive_failures = 2
        status = agent.gateway_status
        assert status["consecutive_failures"] == 2
        assert "healthy" in status
        assert "fail_open" in status
        assert "local_spend_usd" in status

    def test_gateway_unhealthy_after_many_failures_and_staleness(self):
        agent = _make_agent()
        agent._gateway_consecutive_failures = 10
        agent._gateway_last_success = time.monotonic() - 300
        assert agent.gateway_healthy is False

    def test_gateway_healthy_despite_failures_if_recent_success(self):
        agent = _make_agent()
        agent._gateway_consecutive_failures = 10
        agent._gateway_last_success = time.monotonic()  # just now
        assert agent.gateway_healthy is True


# ── _enforce_with_retry ──────────────────────────────────────────────────────


class TestEnforceWithRetry:
    def test_returns_model_when_allowed(self):
        agent = _make_agent()
        agent._call_evaluate = lambda *a, **kw: {"decision": "allow"}
        result = agent._enforce_with_retry("openai", "gpt-4o")
        assert result == "gpt-4o"

    def test_auto_optimize_uses_suggested_model_on_deny(self):
        agent = _make_agent(auto_optimize=True)

        def fake_enforce(provider, model, estimated_tokens=None, estimated_cost=None,
                         resource_type="llm_call", bypass_cache=False):
            if model == "gpt-4o":
                raise PolicyViolationError(
                    decision="deny", reason="Budget exceeded",
                    suggested_model="gpt-4o-mini",
                )
            return None  # allow gpt-4o-mini

        agent.enforce = fake_enforce
        result = agent._enforce_with_retry("openai", "gpt-4o")
        assert result == "gpt-4o-mini"

    def test_auto_optimize_uses_degradation_ladder(self):
        agent = _make_agent(auto_optimize=True)
        agent._call_evaluate = lambda *a, **kw: {
            "decision": "allow", "suggested_model": "gpt-4o-mini"
        }
        result = agent._enforce_with_retry("openai", "gpt-4o")
        assert result == "gpt-4o-mini"

    def test_no_auto_optimize_raises_on_deny(self):
        agent = _make_agent(auto_optimize=False)

        def fake_enforce(**kw):
            raise PolicyViolationError(
                decision="deny", reason="Blocked",
                suggested_model="gpt-4o-mini",
            )

        agent.enforce = fake_enforce
        with pytest.raises(PolicyViolationError):
            agent._enforce_with_retry("openai", "gpt-4o")


# ── Response Cache Integration ───────────────────────────────────────────────


class TestResponseCacheIntegration:
    def test_cache_store_and_check(self):
        agent = _make_agent(response_cache_enabled=True)
        agent._cache_store("openai", "gpt-4o", [{"role": "user", "content": "hi"}], "cached-resp")
        result = agent._cache_check("openai", "gpt-4o", [{"role": "user", "content": "hi"}])
        assert result == "cached-resp"

    def test_cache_miss_returns_none(self):
        agent = _make_agent(response_cache_enabled=True)
        result = agent._cache_check("openai", "gpt-4o", [{"role": "user", "content": "new"}])
        assert result is None

    def test_cache_disabled_store_is_noop(self):
        agent = _make_agent(response_cache_enabled=False)
        agent._cache_store("openai", "gpt-4o", [], "resp")
        assert agent._cache_check("openai", "gpt-4o", []) is None


# ── Anthropic Interceptor ────────────────────────────────────────────────────


class TestAnthropicInterceptor:
    def _setup_fake_anthropic(self):
        """Create and inject a fake anthropic module."""
        anthropic = types.ModuleType("anthropic")
        anthropic.__version__ = "0.30.0"
        resources = types.ModuleType("anthropic.resources")
        anthropic.resources = resources

        class FakeMessages:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                return _mock_response(model="claude-sonnet-4-20250514", input_tokens=100, output_tokens=50)

        class FakeAsyncMessages:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                return _mock_response(model="claude-sonnet-4-20250514", input_tokens=100, output_tokens=50)

        resources.Messages = FakeMessages
        resources.AsyncMessages = FakeAsyncMessages
        return anthropic

    def test_anthropic_sync_instrumentation(self):
        """Verify _instrument_anthropic patches Messages.create and records usage."""
        fake_anthropic = self._setup_fake_anthropic()
        original_create = fake_anthropic.resources.Messages.create

        with patch.dict("sys.modules", {
            "anthropic": fake_anthropic,
            "anthropic.resources": fake_anthropic.resources,
        }):
            agent = _make_agent()
            agent._enforce_with_retry = MagicMock(return_value="claude-sonnet-4-20250514")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_anthropic()

        assert "anthropic" in agent._instrumented
        # The create method should have been replaced
        assert fake_anthropic.resources.Messages.create is not original_create

    def test_anthropic_sync_call_records_usage(self):
        """Calling the patched create records usage data."""
        fake_anthropic = self._setup_fake_anthropic()

        with patch.dict("sys.modules", {
            "anthropic": fake_anthropic,
            "anthropic.resources": fake_anthropic.resources,
        }):
            agent = _make_agent()
            agent._enforce_with_retry = MagicMock(return_value="claude-sonnet-4-20250514")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_anthropic()

            mock_sdk = MagicMock()
            fake_anthropic.resources.Messages.create(
                mock_sdk, model="claude-sonnet-4-20250514",
                messages=[{"role": "user", "content": "hello"}],
            )

        assert len(agent._records) == 1
        assert agent._records[0].provider == "anthropic"
        assert agent._records[0].operation == "messages.create"

    def test_anthropic_import_error_no_crash(self):
        """If anthropic is not installed, instrumentation silently skips."""
        agent = _make_agent()
        with patch.dict("sys.modules", {"anthropic": None}):
            # Force ImportError
            agent._instrument_anthropic()
        assert "anthropic" not in agent._instrumented


# ── OpenAI Interceptor ───────────────────────────────────────────────────────


class TestOpenAIInterceptor:
    def _setup_fake_openai(self):
        """Create and inject a fake openai module."""
        openai = types.ModuleType("openai")
        openai.__version__ = "1.40.0"
        resources = types.ModuleType("openai.resources")
        chat = types.ModuleType("openai.resources.chat")
        openai.resources = resources
        openai.resources.chat = chat

        class FakeCompletions:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                return _mock_response(model="gpt-4o")

        class FakeAsyncCompletions:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                return _mock_response(model="gpt-4o")

        chat.Completions = FakeCompletions
        chat.AsyncCompletions = FakeAsyncCompletions
        return openai

    def test_openai_sync_instrumentation(self):
        fake_openai = self._setup_fake_openai()
        original_create = fake_openai.resources.chat.Completions.create

        with patch.dict("sys.modules", {
            "openai": fake_openai,
            "openai.resources": fake_openai.resources,
            "openai.resources.chat": fake_openai.resources.chat,
        }):
            agent = _make_agent()
            agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_openai()

        assert "openai" in agent._instrumented
        assert fake_openai.resources.chat.Completions.create is not original_create

    def test_openai_sync_call_records_usage(self):
        fake_openai = self._setup_fake_openai()

        with patch.dict("sys.modules", {
            "openai": fake_openai,
            "openai.resources": fake_openai.resources,
            "openai.resources.chat": fake_openai.resources.chat,
        }):
            agent = _make_agent()
            agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_openai()

            mock_sdk = MagicMock()
            # Inject _client for provider detection
            mock_sdk._client._base_url = "https://api.openai.com/v1"
            fake_openai.resources.chat.Completions.create(
                mock_sdk, model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )

        assert len(agent._records) == 1
        assert agent._records[0].provider == "openai"

    def test_openai_xai_provider_detection(self):
        fake_openai = self._setup_fake_openai()

        with patch.dict("sys.modules", {
            "openai": fake_openai,
            "openai.resources": fake_openai.resources,
            "openai.resources.chat": fake_openai.resources.chat,
        }):
            agent = _make_agent()
            agent._enforce_with_retry = MagicMock(return_value="grok-3")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_openai()

            mock_sdk = MagicMock()
            mock_sdk._client._base_url = "https://api.x.ai/v1"
            fake_openai.resources.chat.Completions.create(
                mock_sdk, model="grok-3",
                messages=[{"role": "user", "content": "hi"}],
            )

        assert agent._records[0].provider == "xai"

    def test_openai_import_error_no_crash(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"openai": None}):
            agent._instrument_openai()
        assert "openai" not in agent._instrumented

    def test_openai_response_cache_hit(self):
        """When response cache has a hit, return cached and skip API call."""
        fake_openai = self._setup_fake_openai()

        with patch.dict("sys.modules", {
            "openai": fake_openai,
            "openai.resources": fake_openai.resources,
            "openai.resources.chat": fake_openai.resources.chat,
        }):
            agent = _make_agent(response_cache_enabled=True)
            agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._try_route_sync = MagicMock(return_value=(False, None))

            # Pre-populate cache
            msgs = [{"role": "user", "content": "cached-prompt"}]
            cached_response = _mock_response(model="gpt-4o")
            agent._cache_store("openai", "gpt-4o", msgs, cached_response)

            agent._instrument_openai()

            mock_sdk = MagicMock()
            mock_sdk._client._base_url = "https://api.openai.com/v1"
            result = fake_openai.resources.chat.Completions.create(
                mock_sdk, model="gpt-4o", messages=msgs,
            )

        assert result is cached_response


# ── Google Gemini Interceptor ────────────────────────────────────────────────


class TestGoogleInterceptor:
    def test_google_legacy_instrumentation(self):
        """Test google.generativeai (legacy) instrumentation."""
        # Build fake module hierarchy
        google = types.ModuleType("google")
        genai = types.ModuleType("google.generativeai")
        genai.__version__ = "0.5.0"
        genai_models = types.ModuleType("google.generativeai.generative_models")

        class FakeGenerativeModel:
            model_name = "gemini-2.0-flash"

            @staticmethod
            def generate_content(self_m, *args, **kwargs):
                resp = MagicMock()
                meta = MagicMock()
                meta.prompt_token_count = 50
                meta.candidates_token_count = 20
                resp.usage_metadata = meta
                return resp

        genai_models.GenerativeModel = FakeGenerativeModel
        google.generativeai = genai

        with patch.dict("sys.modules", {
            "google": google,
            "google.generativeai": genai,
            "google.generativeai.generative_models": genai_models,
            "google.genai": None,  # Make new SDK unavailable
        }):
            agent = _make_agent()
            agent._enforce_with_retry = MagicMock(return_value="gemini-2.0-flash")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._instrument_google_genai()

        assert "gemini" in agent._instrumented

    def test_google_legacy_call_records_usage(self):
        google = types.ModuleType("google")
        genai = types.ModuleType("google.generativeai")
        genai.__version__ = "0.5.0"
        genai_models = types.ModuleType("google.generativeai.generative_models")

        class FakeGenerativeModel:
            model_name = "gemini-2.0-flash"

            @staticmethod
            def generate_content(self_m, *args, **kwargs):
                resp = MagicMock()
                meta = MagicMock()
                meta.prompt_token_count = 50
                meta.candidates_token_count = 20
                resp.usage_metadata = meta
                return resp

        genai_models.GenerativeModel = FakeGenerativeModel
        google.generativeai = genai

        with patch.dict("sys.modules", {
            "google": google,
            "google.generativeai": genai,
            "google.generativeai.generative_models": genai_models,
            "google.genai": None,
        }):
            agent = _make_agent()
            agent._enforce_with_retry = MagicMock(return_value="gemini-2.0-flash")
            agent._apply_pii_scan = MagicMock(side_effect=lambda x, **kw: x)
            agent._instrument_google_genai()

            mock_model = MagicMock()
            mock_model.model_name = "gemini-2.0-flash"
            genai_models.GenerativeModel.generate_content(
                mock_model, contents="Hello"
            )

        assert len(agent._records) == 1
        assert agent._records[0].provider == "google"

    def test_google_no_sdk_installed(self):
        """No crash when neither google SDK is installed."""
        agent = _make_agent()
        with patch.dict("sys.modules", {
            "google.genai": None,
            "google.generativeai": None,
        }):
            agent._instrument_google_genai()
        assert "gemini" not in agent._instrumented


# ── Groq Interceptor ────────────────────────────────────────────────────────


class TestGroqInterceptor:
    def _setup_fake_groq(self):
        groq = types.ModuleType("groq")
        groq.__version__ = "0.5.0"
        resources = types.ModuleType("groq.resources")
        chat = types.ModuleType("groq.resources.chat")
        groq.resources = resources
        groq.resources.chat = chat

        class FakeCompletions:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                return _mock_response(model="llama-3-70b")

        chat.Completions = FakeCompletions
        return groq

    def test_groq_instrumentation(self):
        fake_groq = self._setup_fake_groq()

        with patch.dict("sys.modules", {
            "groq": fake_groq,
            "groq.resources": fake_groq.resources,
            "groq.resources.chat": fake_groq.resources.chat,
        }):
            agent = _make_agent()
            agent.enforce = MagicMock(return_value=None)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_groq()

        assert "groq" in agent._instrumented

    def test_groq_call_records_usage(self):
        fake_groq = self._setup_fake_groq()

        with patch.dict("sys.modules", {
            "groq": fake_groq,
            "groq.resources": fake_groq.resources,
            "groq.resources.chat": fake_groq.resources.chat,
        }):
            agent = _make_agent()
            agent.enforce = MagicMock(return_value=None)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_groq()

            mock_sdk = MagicMock()
            fake_groq.resources.chat.Completions.create(
                mock_sdk, model="llama-3-70b",
                messages=[{"role": "user", "content": "hi"}],
            )

        assert len(agent._records) == 1
        assert agent._records[0].provider == "groq"


# ── Mistral Interceptor ──────────────────────────────────────────────────────


class TestMistralInterceptor:
    def test_mistral_legacy_instrumentation(self):
        """Test legacy MistralClient instrumentation."""
        mistralai = types.ModuleType("mistralai")
        mistralai.__version__ = "0.4.0"
        client_mod = types.ModuleType("mistralai.client")

        class FakeMistralClient:
            @staticmethod
            def chat(self_sdk, *args, **kwargs):
                return _mock_response(model="mistral-large")

        client_mod.MistralClient = FakeMistralClient
        mistralai.client = client_mod

        with patch.dict("sys.modules", {
            "mistralai": mistralai,
            "mistralai.client": client_mod,
            "mistralai.resources.chat": None,  # no new SDK
        }):
            agent = _make_agent()
            agent.enforce = MagicMock(return_value=None)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_mistral()

        assert "mistral" in agent._instrumented


# ── Cohere Interceptor ───────────────────────────────────────────────────────


class TestCohereInterceptor:
    def test_cohere_instrumentation(self):
        cohere = types.ModuleType("cohere")
        cohere.__version__ = "5.0.0"

        class FakeClient:
            @staticmethod
            def chat(self_sdk, *args, **kwargs):
                resp = MagicMock()
                resp.usage = None
                resp.meta = None
                return resp

        cohere.Client = FakeClient

        with patch.dict("sys.modules", {"cohere": cohere}):
            agent = _make_agent()
            agent.enforce = MagicMock(return_value=None)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_cohere()

        assert "cohere" in agent._instrumented

    def test_cohere_call_records_usage(self):
        cohere = types.ModuleType("cohere")
        cohere.__version__ = "5.0.0"

        class FakeClient:
            @staticmethod
            def chat(self_sdk, *args, **kwargs):
                resp = MagicMock()
                tokens = MagicMock()
                tokens.input_tokens = 40
                tokens.output_tokens = 15
                usage = MagicMock()
                usage.tokens = tokens
                resp.usage = usage
                resp.meta = None
                return resp

        cohere.Client = FakeClient

        with patch.dict("sys.modules", {"cohere": cohere}):
            agent = _make_agent()
            agent.enforce = MagicMock(return_value=None)
            agent._try_route_sync = MagicMock(return_value=(False, None))
            agent._instrument_cohere()

            mock_sdk = MagicMock()
            cohere.Client.chat(mock_sdk, model="command-r-plus")

        assert len(agent._records) == 1
        assert agent._records[0].provider == "cohere"


# ── Flush (with network mock) ───────────────────────────────────────────────


class TestFlushWithNetwork:
    def test_flush_raw_sends_payload(self):
        agent = _make_agent(aggregation_enabled=False)
        agent.record(provider="openai", model="gpt-4o", input_tokens=100, output_tokens=50)

        captured_payload = {}

        def fake_urlopen(req, timeout=None, context=None):
            body = json.loads(req.data.decode())
            captured_payload.update(body)
            resp = MagicMock()
            resp.read.return_value = b'{"accepted": 1}'
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            agent._flush_raw()

        assert "records" in captured_payload
        assert len(captured_payload["records"]) == 1
        assert captured_payload["records"][0]["provider"] == "openai"

    def test_flush_raw_failure_requeues(self):
        agent = _make_agent(aggregation_enabled=False)
        agent.record(provider="openai", model="gpt-4o", input_tokens=100, output_tokens=50)

        def fake_urlopen(req, timeout=None, context=None):
            raise ConnectionError("Network error")

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            agent._flush_raw()

        # Records should be re-queued
        assert len(agent._records) == 1

    def test_flush_aggregated_sends_payload(self):
        agent = _make_agent(aggregation_enabled=True, trace_sample_rate=0.0)
        agent.record(provider="openai", model="gpt-4o", input_tokens=100, output_tokens=50)
        agent.record(provider="openai", model="gpt-4o", input_tokens=200, output_tokens=100)

        captured_payload = {}

        def fake_urlopen(req, timeout=None, context=None):
            body = json.loads(req.data.decode())
            captured_payload.update(body)
            resp = MagicMock()
            resp.read.return_value = b'{"accepted": 2}'
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            agent._flush_aggregated()

        assert captured_payload["format"] == "aggregated"
        assert len(captured_payload["aggregates"]) == 1
        assert captured_payload["aggregates"][0]["call_count"] == 2

    def test_flush_aggregated_failure_requeues(self):
        agent = _make_agent(aggregation_enabled=True, trace_sample_rate=0.0)
        agent.record(provider="openai", model="gpt-4o", input_tokens=100, output_tokens=50)

        def fake_urlopen(req, timeout=None, context=None):
            raise ConnectionError("Network error")

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            agent._flush_aggregated()

        # Buckets should be re-queued
        assert len(agent._agg_buckets) == 1


# ── Self-registration ────────────────────────────────────────────────────────


class TestSelfRegistration:
    def test_successful_registration(self):
        agent = _make_agent()
        agent._api_key = None  # Reset so we test registration flow

        resp_body = json.dumps({
            "api_key": "mds_new_key",
            "app_uuid": "uuid-123",
            "app_id": "my-app",
            "team_id": "team-1",
            "registered": True,
            "team_slug": "my-team",
        }).encode()

        def fake_urlopen(req, timeout=None, context=None):
            resp = MagicMock()
            resp.read.return_value = resp_body
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._self_register()

        assert result is True
        assert agent._api_key == "mds_new_key"
        assert agent._app_id == "my-app"

    def test_failed_registration(self):
        agent = _make_agent()
        agent._api_key = None

        def fake_urlopen(req, timeout=None, context=None):
            raise ConnectionError("Refused")

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            result = agent._self_register()

        assert result is False


# ── Policy Sync ──────────────────────────────────────────────────────────────


class TestPolicySync:
    def test_sync_policies_success(self):
        agent = _make_agent()

        resp_body = json.dumps({
            "policies": [
                {"policy_type": "model_denylist", "config": {"models": ["gpt-4"]},
                 "effect": "deny", "name": "no-gpt4"},
            ]
        }).encode()

        def fake_urlopen(req, timeout=None, context=None):
            resp = MagicMock()
            resp.read.return_value = resp_body
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            agent._sync_policies()

        assert len(agent._local_policies) == 1
        assert agent._local_policies[0]["name"] == "no-gpt4"

    def test_sync_policies_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        agent._sync_policies()
        assert agent._local_policies == []

    def test_sync_policies_failure(self):
        agent = _make_agent()

        def fake_urlopen(req, timeout=None, context=None):
            raise ConnectionError("Refused")

        with patch("modus.agent._urlopen_tls", side_effect=fake_urlopen):
            agent._sync_policies()  # Should not raise


# ── build_prompt decorator/context manager ───────────────────────────────────


class TestBuildPrompt:
    def test_build_prompt_as_decorator_with_fn(self):
        agent = _make_agent()

        @agent.build_prompt
        def my_fn():
            return 42

        assert my_fn() == 42

    def test_build_prompt_as_decorator_with_name(self):
        agent = _make_agent()

        @agent.build_prompt(name="planner")
        def my_fn():
            return 99

        assert my_fn() == 99

    def test_build_prompt_as_context_manager(self):
        agent = _make_agent()
        with agent.build_prompt(name="test"):
            pass  # Should not raise

    def test_build_prompt_callable_name_raises(self):
        agent = _make_agent()
        with pytest.raises(TypeError, match="keyword argument"):
            agent.build_prompt(name=lambda: None)


# ── _rec (internal recording shorthand) ──────────────────────────────────────


class TestRecInternal:
    def test_rec_injects_span_metadata(self):
        agent = _make_agent()
        with agent.session(session_id="s1"):
            agent._rec("openai", "gpt-4o", "chat", 100, 50, 200)

        assert len(agent._records) == 1
        meta = agent._records[0].metadata
        assert meta is not None
        assert meta["mds_session_id"] == "s1"

    def test_rec_no_session_no_span_meta(self):
        agent = _make_agent()
        agent._rec("openai", "gpt-4o", "chat", 100, 50, 200)

        assert len(agent._records) == 1
        # No span metadata when no session
        meta = agent._records[0].metadata
        assert meta is None or "mds_session_id" not in (meta or {})


# ── Extract prompts for routing ──────────────────────────────────────────────


class TestExtractPromptsForRouting:
    def test_anthropic_extraction(self):
        system, user = ModusAgent._extract_prompts_for_routing(
            "anthropic",
            {
                "system": "You are a helpful assistant",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert system == "You are a helpful assistant"
        assert user == "Hello"

    def test_openai_extraction(self):
        system, user = ModusAgent._extract_prompts_for_routing(
            "openai",
            {
                "messages": [
                    {"role": "system", "content": "Be concise"},
                    {"role": "user", "content": "Summarize this"},
                ],
            },
        )
        assert system == "Be concise"
        assert user == "Summarize this"

    def test_anthropic_list_system(self):
        system, user = ModusAgent._extract_prompts_for_routing(
            "anthropic",
            {
                "system": [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}],
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert "Part 1" in system
        assert "Part 2" in system

    def test_empty_kwargs(self):
        system, user = ModusAgent._extract_prompts_for_routing("openai", {})
        assert system == ""
        assert user == ""
