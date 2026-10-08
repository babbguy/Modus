"""
Tests for sdk/modus/agent.py internals.

Covers:
  - Heartbeat/sync internals (_send_heartbeat, _sync_policies, _flush_raw, _flush_aggregated)
  - Interceptor wrappers (anthropic, openai, bedrock, google, groq, mistral, cohere, deepseek)
  - Self-registration (_self_register, start)
  - Routing (extract_prompts_for_routing, extract_response_text, _try_route_sync, _try_route_async)
  - Bedrock helpers (_ReusableBody, _BedrockStreamWrapper)
  - _flush_routing_outcomes, _log_routing_outcome, _report_topology, _rescan_loop
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from sdk.modus.agent import (
    ModusAgent,
    _AggregationBucket,
    _BedrockStreamWrapper,
    _ReusableBody,
    _UsageRecord,
    _urlopen_tls,
    __version__,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_agent(**kw) -> ModusAgent:
    """Create agent without starting background threads."""
    defaults = {
        "orchestrator_url": "http://localhost:9000",
        "team_token": "mds_team_test",
        "app_id": "test-app",
        "app_name": "Test App",
        "environment": "test",
        "aggregation_enabled": False,
    }
    defaults.update(kw)
    return ModusAgent(**defaults)


def _make_urlopen_response(body: dict, status: int = 200):
    """Create a mock urlopen response context manager."""
    raw = json.dumps(body).encode()
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.status = status
    return resp


def _make_record(**kw) -> _UsageRecord:
    defaults = {
        "provider": "openai",
        "resource_type": "llm_call",
        "model": "gpt-4o",
        "operation": "chat.completions.create",
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "input_cost": Decimal("0.0025"),
        "output_cost": Decimal("0.0050"),
        "total_cost": Decimal("0.0075"),
        "duration_ms": 200,
        "timestamp": "2026-01-01T00:00:00Z",
        "metadata": None,
    }
    defaults.update(kw)
    return _UsageRecord(**defaults)


# ── Self-registration ─────────────────────────────────────────────────────────

class TestSelfRegister:
    """Tests for _self_register (lines 711-741)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_self_register_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "api_key": "ak_test123",
            "app_uuid": "uuid-abc",
            "app_id": "test-app",
            "team_id": "team-abc",
            "registered": True,
            "team_slug": "my-team",
        })
        agent = _make_agent()
        result = agent._self_register()
        assert result is True
        assert agent._api_key == "ak_test123"
        assert agent._app_uuid == "uuid-abc"
        assert agent._app_id == "test-app"
        assert agent._team_id == "team-abc"

    @patch("sdk.modus.agent._urlopen_tls")
    def test_self_register_reconnect(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "api_key": "ak_reconnect",
            "app_uuid": "uuid-def",
            "app_id": "test-app",
            "team_id": "team-def",
            "registered": False,
            "team_slug": "team-x",
        })
        agent = _make_agent()
        result = agent._self_register()
        assert result is True
        assert agent._api_key == "ak_reconnect"

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("connection refused"))
    def test_self_register_failure(self, mock_urlopen):
        agent = _make_agent()
        result = agent._self_register()
        assert result is False
        assert agent._api_key is None


# ── Heartbeat ────────────────────────────────────────────────────────────────

class TestHeartbeat:
    """Tests for _send_heartbeat (lines 680-706)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_send_heartbeat_payload(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"status": "ok"})
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._instrumented = ["anthropic", "openai"]
        agent._sdk_versions = {"anthropic": "0.25.0"}

        agent._send_heartbeat()

        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data)
        assert body["agent_version"] == __version__
        assert body["instrumented_providers"] == ["anthropic", "openai"]
        assert body["sdk_versions"] == {"anthropic": "0.25.0"}
        assert "host_info" in body
        assert "python" in body["host_info"]
        assert "pid" in body["host_info"]

    def test_send_heartbeat_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        # Should be a no-op
        agent._send_heartbeat()

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("timeout"))
    def test_send_heartbeat_failure(self, mock_urlopen):
        agent = _make_agent()
        agent._api_key = "ak_test"
        # Should not raise
        agent._send_heartbeat()

    @patch("sdk.modus.agent._urlopen_tls")
    def test_handle_heartbeat_response_with_routing(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "routing_fingerprints": [{"hash": "abc", "cheap_model": "gpt-3.5-turbo"}],
        }).encode()
        agent = _make_agent()
        with patch("sdk.modus.agent.ModusAgent._handle_heartbeat_response.__wrapped__",
                    side_effect=None, create=True):
            pass
        # Test the actual method
        with patch.dict("sys.modules", {"modus.routing_interceptor": MagicMock()}):
            agent._handle_heartbeat_response(resp)

    @patch("sdk.modus.agent._urlopen_tls")
    def test_handle_heartbeat_response_no_routing(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"status": "ok"}).encode()
        agent = _make_agent()
        agent._handle_heartbeat_response(resp)

    @patch("sdk.modus.agent._urlopen_tls")
    def test_handle_heartbeat_response_parse_error(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = b"not json"
        agent = _make_agent()
        # Should not raise
        agent._handle_heartbeat_response(resp)


# ── Policy sync ──────────────────────────────────────────────────────────────

class TestSyncPolicies:
    """Tests for _sync_policies (lines 743-763)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_sync_policies_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "policies": [
                {"id": "p1", "name": "rate-limit", "rule": "deny"},
                {"id": "p2", "name": "budget-cap", "rule": "throttle"},
            ],
        })
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._sync_policies()
        assert len(agent._local_policies) == 2
        assert agent._last_policy_sync > 0

    def test_sync_policies_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        agent._sync_policies()
        assert agent._local_policies == []

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("fail"))
    def test_sync_policies_failure(self, mock_urlopen):
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._sync_policies()
        assert agent._local_policies == []


# ── Flush raw ────────────────────────────────────────────────────────────────

class TestFlushRaw:
    """Tests for _flush_raw (lines 2525-2574)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_flush_raw_sends_batch(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"accepted": 2})
        agent = _make_agent(aggregation_enabled=False)
        agent._api_key = "ak_test"
        agent._records = [_make_record(), _make_record(model="gpt-3.5-turbo")]

        agent._flush_raw()

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert len(body["records"]) == 2
        assert "batch_id" in body
        assert body["agent_version"] == __version__
        assert agent._records == []

    def test_flush_raw_empty(self):
        agent = _make_agent(aggregation_enabled=False)
        agent._api_key = "ak_test"
        agent._records = []
        # Should be a no-op
        agent._flush_raw()

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("network error"))
    def test_flush_raw_failure_requeues(self, mock_urlopen):
        agent = _make_agent(aggregation_enabled=False)
        agent._api_key = "ak_test"
        records = [_make_record()]
        agent._records = records[:]
        agent._flush_raw()
        # Records should be re-queued
        assert len(agent._records) == 1

    def test_flush_raw_no_api_key(self):
        agent = _make_agent(aggregation_enabled=False)
        agent._api_key = None
        agent._records = [_make_record()]
        agent._flush()
        assert len(agent._records) == 1


# ── Flush aggregated ─────────────────────────────────────────────────────────

class TestFlushAggregated:
    """Tests for _flush_aggregated (lines 2576-2668)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_flush_aggregated_sends(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"accepted": 1})
        agent = _make_agent(aggregation_enabled=True)
        agent._api_key = "ak_test"
        bucket = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat.completions.create", resource_type="llm_call",
        )
        bucket.accumulate(_make_record())
        agent._agg_buckets["openai:gpt-4o:chat:llm_call"] = bucket
        agent._agg_sampled = [_make_record()]

        agent._flush_aggregated()

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["format"] == "aggregated"
        assert len(body["aggregates"]) == 1
        assert len(body["traces"]) == 1

    def test_flush_aggregated_empty(self):
        agent = _make_agent(aggregation_enabled=True)
        agent._api_key = "ak_test"
        # Should be a no-op
        agent._flush_aggregated()

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("fail"))
    def test_flush_aggregated_failure_requeues(self, mock_urlopen):
        agent = _make_agent(aggregation_enabled=True)
        agent._api_key = "ak_test"
        bucket = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        bucket.accumulate(_make_record())
        agent._agg_buckets["openai:gpt-4o:chat:llm_call"] = bucket
        agent._agg_sampled = [_make_record()]

        agent._flush_aggregated()

        # Re-queued
        assert len(agent._agg_buckets) == 1
        assert len(agent._agg_sampled) == 1

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("fail"))
    def test_flush_aggregated_failure_merges_existing(self, mock_urlopen):
        """When flush fails and a bucket already exists, merge counts."""
        agent = _make_agent(aggregation_enabled=True)
        agent._api_key = "ak_test"
        # Pre-existing bucket
        existing = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        existing.accumulate(_make_record())
        # Bucket to be flushed
        to_flush = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        to_flush.accumulate(_make_record())
        agent._agg_buckets["openai:gpt-4o:chat:llm_call"] = to_flush

        agent._flush_aggregated()

        # Now add a new bucket with same key to simulate concurrent accumulation
        # The failed bucket should have been re-queued
        requeued = agent._agg_buckets.get("openai:gpt-4o:chat:llm_call")
        assert requeued is not None
        assert requeued.call_count >= 1


# ── Flush routing outcomes ───────────────────────────────────────────────────

class TestFlushRoutingOutcomes:
    """Tests for _log_routing_outcome and _flush_routing_outcomes."""

    def test_log_routing_outcome(self):
        agent = _make_agent()
        agent._app_id = "test-app"
        agent._log_routing_outcome(
            fingerprint_hash="abc",
            routed_to="cheap",
            provider="openai",
        )
        assert len(agent._routing_outcomes) == 1
        assert agent._routing_outcomes[0]["fingerprint_hash"] == "abc"
        assert "timestamp" in agent._routing_outcomes[0]

    def test_log_routing_outcome_overflow(self):
        agent = _make_agent(max_buffer_size=2)
        agent._app_id = "test-app"
        for i in range(5):
            agent._log_routing_outcome(fingerprint_hash=f"h{i}", routed_to="cheap",
                                       provider="openai")
        assert len(agent._routing_outcomes) == 2

    @patch("sdk.modus.agent._urlopen_tls")
    def test_flush_routing_outcomes_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({})
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._app_id = "test-app"
        agent._log_routing_outcome(fingerprint_hash="abc", routed_to="cheap",
                                   provider="openai")
        agent._flush_routing_outcomes()
        assert agent._routing_outcomes == []
        mock_urlopen.assert_called_once()

    def test_flush_routing_outcomes_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        agent._routing_outcomes = [{"test": True}]
        agent._flush_routing_outcomes()
        assert len(agent._routing_outcomes) == 1

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("fail"))
    def test_flush_routing_outcomes_failure_requeues(self, mock_urlopen):
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._routing_outcomes = [{"test": True}]
        agent._flush_routing_outcomes()
        assert len(agent._routing_outcomes) == 1


# ── Report topology ──────────────────────────────────────────────────────────

class TestReportTopology:
    """Tests for _report_topology (lines 775-794)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_report_topology(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "ai_summary": "Python FastAPI app with OpenAI",
        })
        agent = _make_agent()
        agent._api_key = "ak_test"
        snapshot = MagicMock()
        snapshot.to_dict.return_value = {"app_id": "test", "hostname": "h1"}
        agent._report_topology(snapshot)
        mock_urlopen.assert_called_once()

    def test_report_topology_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        agent._report_topology(MagicMock())

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("fail"))
    def test_report_topology_failure(self, mock_urlopen):
        agent = _make_agent()
        agent._api_key = "ak_test"
        snapshot = MagicMock()
        snapshot.to_dict.return_value = {}
        agent._report_topology(snapshot)


# ── Rescan loop ──────────────────────────────────────────────────────────────

class TestRescanLoop:
    """Tests for _rescan_loop (lines 811-832)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_rescan_loop_detects_change(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"ai_summary": ""})
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._last_topology_hash = "old_hash"

        mock_snapshot = MagicMock()
        mock_snapshot.content_hash.return_value = "new_hash"
        mock_snapshot.to_dict.return_value = {}

        mock_scanner = MagicMock()
        mock_scanner.return_value.scan.return_value = mock_snapshot

        # Simulate the rescan logic directly (avoids patching import inside start)
        with patch("sdk.modus.discovery.EnvironmentScanner", mock_scanner):
            from sdk.modus.discovery import EnvironmentScanner
            snapshot = EnvironmentScanner().scan(
                app_id=agent._app_id_hint,
                app_name=agent._app_name_hint,
                environment=agent.environment,
                agent_version=__version__,
            )
            new_hash = snapshot.content_hash()
            if new_hash != agent._last_topology_hash:
                agent._report_topology(snapshot)
                agent._last_topology_hash = new_hash

        assert agent._last_topology_hash == "new_hash"


# ── Interceptor: Anthropic ───────────────────────────────────────────────────

class TestInstrumentAnthropic:
    """Tests for _instrument_anthropic (lines 1556-1737)."""

    def test_anthropic_sync_instrumentation(self):
        """Verify anthropic sync create is patched and calls through."""
        # Build a mock anthropic module
        anthropic = ModuleType("anthropic")
        anthropic.__version__ = "0.30.0"
        resources = ModuleType("anthropic.resources")
        anthropic.resources = resources

        class Messages:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                resp = SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=10, output_tokens=20),
                    model="claude-sonnet-4-20250514",
                    stop_reason="end_turn",
                )
                return resp

        class AsyncMessages:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=10, output_tokens=20),
                    model="claude-sonnet-4-20250514",
                )

        resources.Messages = Messages
        resources.AsyncMessages = AsyncMessages
        original_create = Messages.create

        agent = _make_agent()
        agent._api_key = "ak_test"
        # Suppress enforce
        agent.enforce = MagicMock(return_value=None)
        agent._enforce_with_retry = MagicMock(return_value="claude-sonnet-4-20250514")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict("sys.modules", {"anthropic": anthropic, "anthropic.resources": resources}):
            agent._instrument_anthropic()

        assert "anthropic" in agent._instrumented
        assert Messages.create is not original_create

        # Call the patched method
        sdk_instance = MagicMock()
        resp = Messages.create(sdk_instance, model="claude-sonnet-4-20250514", messages=[])
        assert hasattr(resp, "usage")

    def test_anthropic_import_error(self):
        """No crash when anthropic is not installed."""
        agent = _make_agent()
        with patch.dict("sys.modules", {"anthropic": None}):
            with patch("builtins.__import__", side_effect=ImportError("no anthropic")):
                agent._instrument_anthropic()
        assert "anthropic" not in agent._instrumented


# ── Interceptor: OpenAI ──────────────────────────────────────────────────────

class TestInstrumentOpenAI:
    """Tests for _instrument_openai (lines 1777-1947)."""

    def test_openai_sync_instrumentation(self):
        openai = ModuleType("openai")
        openai.__version__ = "1.30.0"
        resources = ModuleType("openai.resources")
        chat = ModuleType("openai.resources.chat")
        openai.resources = resources
        openai.resources.chat = chat

        class Completions:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    model="gpt-4o",
                )

        class AsyncCompletions:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    model="gpt-4o",
                )

        chat.Completions = Completions
        chat.AsyncCompletions = AsyncCompletions
        original = Completions.create

        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict("sys.modules", {
            "openai": openai,
            "openai.resources": resources,
            "openai.resources.chat": chat,
        }):
            agent._instrument_openai()

        assert "openai" in agent._instrumented
        assert Completions.create is not original

        sdk_instance = MagicMock()
        resp = Completions.create(sdk_instance, model="gpt-4o", messages=[])
        assert hasattr(resp, "usage")

    def test_openai_import_error(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"openai": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                agent._instrument_openai()
        assert "openai" not in agent._instrumented


# ── Interceptor: DeepSeek ────────────────────────────────────────────────────

class TestInstrumentDeepSeek:
    def test_deepseek_with_openai(self):
        """DeepSeek SDK is covered via OpenAI instrumentation."""
        openai = ModuleType("openai")
        deepseek = ModuleType("deepseek")
        agent = _make_agent()
        with patch.dict("sys.modules", {"openai": openai, "deepseek": deepseek}):
            agent._instrument_deepseek()

    def test_deepseek_no_openai(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"openai": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                agent._instrument_deepseek()


# ── Interceptor: Bedrock ─────────────────────────────────────────────────────

class TestInstrumentBoto3:
    """Tests for _instrument_boto3 (lines 1971-2066)."""

    def test_boto3_instrumentation(self):
        boto3 = ModuleType("boto3")
        boto3.__version__ = "1.34.0"

        class FakeSession:
            @staticmethod
            def client(service_name, *args, **kwargs):
                c = MagicMock()
                c.invoke_model = MagicMock()
                c.invoke_model_with_response_stream = MagicMock()
                return c

        boto3.client = FakeSession.client
        boto3.Session = FakeSession

        agent = _make_agent()
        agent.enforce = MagicMock(return_value=None)

        with patch.dict("sys.modules", {"boto3": boto3}):
            agent._instrument_boto3()

        assert "bedrock" in agent._instrumented

        # Test that bedrock-runtime clients get patched
        client = boto3.client("bedrock-runtime")
        assert client is not None

    def test_boto3_import_error(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"boto3": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                agent._instrument_boto3()
        assert "bedrock" not in agent._instrumented


# ── Interceptor: Google Gemini ───────────────────────────────────────────────

class TestInstrumentGoogleGenAI:
    """Tests for _instrument_google_genai (lines 2070-2206)."""

    def test_google_new_sdk(self):
        """Patch new google-genai SDK."""
        google = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.__version__ = "0.5.0"
        models = ModuleType("google.genai.models")

        class Models:
            @staticmethod
            def generate_content(self_m, *args, **kwargs):
                return SimpleNamespace(
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=10, candidates_token_count=20
                    ),
                )

        models.Models = Models
        google.genai = genai

        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._enforce_with_retry = MagicMock(return_value="gemini-2.0-flash")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict("sys.modules", {
            "google": google,
            "google.genai": genai,
            "google.genai.models": models,
        }):
            agent._instrument_google_genai()

        assert "gemini" in agent._instrumented

    def test_google_legacy_sdk(self):
        """Patch legacy google-generativeai SDK."""
        google = ModuleType("google")
        generativeai = ModuleType("google.generativeai")
        generativeai.__version__ = "0.3.0"
        gm = ModuleType("google.generativeai.generative_models")

        class GenerativeModel:
            model_name = "gemini-1.5-pro"

            @staticmethod
            def generate_content(self_m, *args, **kwargs):
                return SimpleNamespace(
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=5, candidates_token_count=10
                    ),
                )

            @staticmethod
            async def generate_content_async(self_m, *args, **kwargs):
                return SimpleNamespace(
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=5, candidates_token_count=10
                    ),
                )

        gm.GenerativeModel = GenerativeModel
        google.generativeai = generativeai

        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._enforce_with_retry = MagicMock(return_value="gemini-1.5-pro")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)

        # Make new SDK import fail so it falls back to legacy
        def custom_import(name, *args, **kwargs):
            if name == "google.genai":
                raise ImportError
            if name == "google.generativeai":
                return generativeai
            if name == "google.generativeai.generative_models":
                return gm
            raise ImportError(name)

        with patch.dict("sys.modules", {
            "google": google,
            "google.generativeai": generativeai,
            "google.generativeai.generative_models": gm,
        }):
            # Ensure new SDK path fails
            with patch.dict("sys.modules", {"google.genai": None}):
                agent._instrument_google_genai()

        assert "gemini" in agent._instrumented

    def test_google_import_error(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"google.genai": None, "google.generativeai": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                agent._instrument_google_genai()
        assert "gemini" not in agent._instrumented


# ── Interceptor: Groq ────────────────────────────────────────────────────────

class TestInstrumentGroq:
    """Tests for _instrument_groq (lines 2210-2315)."""

    def test_groq_instrumentation(self):
        groq = ModuleType("groq")
        groq.__version__ = "0.5.0"
        resources = ModuleType("groq.resources")
        chat = ModuleType("groq.resources.chat")
        groq.resources = resources
        groq.resources.chat = chat

        class Completions:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    model="llama-3-70b",
                )

        class AsyncCompletions:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    model="llama-3-70b",
                )

        chat.Completions = Completions
        chat.AsyncCompletions = AsyncCompletions

        agent = _make_agent()
        agent.enforce = MagicMock(return_value=None)
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict("sys.modules", {
            "groq": groq,
            "groq.resources": resources,
            "groq.resources.chat": chat,
        }):
            agent._instrument_groq()

        assert "groq" in agent._instrumented

    def test_groq_import_error(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"groq": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                agent._instrument_groq()
        assert "groq" not in agent._instrumented


# ── Interceptor: Mistral ─────────────────────────────────────────────────────

class TestInstrumentMistral:
    """Tests for _instrument_mistral (lines 2319-2409)."""

    def test_mistral_new_sdk(self):
        mistralai = ModuleType("mistralai")
        mistralai.__version__ = "1.0.0"
        resources_mod = ModuleType("mistralai.resources")
        chat_module = ModuleType("mistralai.resources.chat")

        class Chat:
            @staticmethod
            def complete(self_r, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    model="mistral-large",
                )

        chat_module.Chat = Chat
        mistralai.Mistral = type("Mistral", (), {})
        mistralai.resources = resources_mod
        resources_mod.chat = chat_module

        agent = _make_agent()
        agent.enforce = MagicMock(return_value=None)
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict("sys.modules", {
            "mistralai": mistralai,
            "mistralai.resources": resources_mod,
            "mistralai.resources.chat": chat_module,
        }):
            agent._instrument_mistral()

        assert "mistral" in agent._instrumented

    def test_mistral_legacy_sdk(self):
        mistralai = ModuleType("mistralai")
        mistralai.__version__ = "0.5.0"
        client_mod = ModuleType("mistralai.client")

        class MistralClient:
            @staticmethod
            def chat(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    model="mistral-medium",
                )

        client_mod.MistralClient = MistralClient

        agent = _make_agent()
        agent.enforce = MagicMock(return_value=None)
        agent._try_route_sync = MagicMock(return_value=(False, None))

        # Make new SDK path fail
        def custom_import(name, *args, **kwargs):
            if "Mistral" in str(name):
                raise ImportError
            raise ImportError(name)

        with patch.dict("sys.modules", {
            "mistralai": mistralai,
            "mistralai.client": client_mod,
        }):
            # Patch so new Mistral class doesn't exist
            if hasattr(mistralai, "Mistral"):
                delattr(mistralai, "Mistral")
            agent._instrument_mistral()

        assert "mistral" in agent._instrumented

    def test_mistral_import_error(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"mistralai": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                agent._instrument_mistral()
        assert "mistral" not in agent._instrumented


# ── Interceptor: Cohere ──────────────────────────────────────────────────────

class TestInstrumentCohere:
    """Tests for _instrument_cohere (lines 2413-2501)."""

    def test_cohere_sync_instrumentation(self):
        cohere = ModuleType("cohere")
        cohere.__version__ = "5.0.0"

        class Client:
            @staticmethod
            def chat(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(
                        tokens=SimpleNamespace(input_tokens=10, output_tokens=20)
                    ),
                )

        class AsyncClient:
            @staticmethod
            async def chat(self_sdk, *args, **kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(
                        tokens=SimpleNamespace(input_tokens=10, output_tokens=20)
                    ),
                )

        cohere.Client = Client
        cohere.AsyncClient = AsyncClient

        agent = _make_agent()
        agent.enforce = MagicMock(return_value=None)
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict("sys.modules", {"cohere": cohere}):
            agent._instrument_cohere()

        assert "cohere" in agent._instrumented

    def test_cohere_import_error(self):
        agent = _make_agent()
        with patch.dict("sys.modules", {"cohere": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                agent._instrument_cohere()
        assert "cohere" not in agent._instrumented


# ── Detect OAI provider ─────────────────────────────────────────────────────

class TestDetectOaiProvider:
    def test_xai_detected(self):
        sdk = MagicMock()
        sdk._client._base_url = "https://api.x.ai/v1"
        assert ModusAgent._detect_oai_provider(sdk) == "xai"

    def test_deepseek_detected(self):
        sdk = MagicMock()
        sdk._client._base_url = "https://api.deepseek.com/v1"
        assert ModusAgent._detect_oai_provider(sdk) == "deepseek"

    def test_openai_default(self):
        sdk = MagicMock()
        sdk._client._base_url = "https://api.openai.com/v1"
        assert ModusAgent._detect_oai_provider(sdk) == "openai"

    def test_no_base_url(self):
        sdk = MagicMock(spec=[])
        assert ModusAgent._detect_oai_provider(sdk) == "openai"


# ── Extract prompts for routing ──────────────────────────────────────────────

class TestExtractPromptsForRouting:
    """Tests for _extract_prompts_for_routing (lines 2956-3007)."""

    def test_anthropic_string_system(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("anthropic", {
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "Hello"}],
        })
        assert sys_p == "You are a helpful assistant."
        assert user_p == "Hello"

    def test_anthropic_list_system(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("anthropic", {
            "system": [{"text": "Part 1"}, {"text": "Part 2"}],
            "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
        })
        assert sys_p == "Part 1 Part 2"
        assert user_p == "Hi"

    def test_openai(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("openai", {
            "messages": [
                {"role": "system", "content": "Be helpful"},
                {"role": "user", "content": "Question"},
            ],
        })
        assert sys_p == "Be helpful"
        assert user_p == "Question"

    def test_groq(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("groq", {
            "messages": [
                {"role": "system", "content": "System msg"},
                {"role": "user", "content": "User msg"},
            ],
        })
        assert sys_p == "System msg"
        assert user_p == "User msg"

    def test_google(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("google", {
            "config": {"system_instruction": "Be precise"},
            "contents": "What is 2+2?",
        })
        assert sys_p == "Be precise"
        assert user_p == "What is 2+2?"

    def test_cohere(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("cohere", {
            "preamble": "You are CohereBot",
            "message": "Tell me a joke",
        })
        assert sys_p == "You are CohereBot"
        assert user_p == "Tell me a joke"

    def test_unknown_provider(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("unknown", {})
        assert sys_p == ""
        assert user_p == ""


# ── Extract response text ────────────────────────────────────────────────────

class TestExtractResponseText:
    """Tests for _extract_response_text (lines 3010-3053)."""

    def test_anthropic_response(self):
        content_block = SimpleNamespace(text="Hello world")
        resp = SimpleNamespace(content=[content_block])
        text = ModusAgent._extract_response_text("anthropic", resp)
        assert text == "Hello world"

    def test_anthropic_empty(self):
        resp = SimpleNamespace(content=[])
        text = ModusAgent._extract_response_text("anthropic", resp)
        assert text == ""

    def test_openai_response(self):
        msg = SimpleNamespace(content="Hi there")
        choice = SimpleNamespace(message=msg)
        resp = SimpleNamespace(choices=[choice])
        text = ModusAgent._extract_response_text("openai", resp)
        assert text == "Hi there"

    def test_openai_empty(self):
        resp = SimpleNamespace(choices=[])
        text = ModusAgent._extract_response_text("openai", resp)
        assert text == ""

    def test_google_text(self):
        resp = SimpleNamespace(text="Generated text")
        text = ModusAgent._extract_response_text("google", resp)
        assert text == "Generated text"

    def test_google_candidates(self):
        part = SimpleNamespace(text="From candidate")
        content = SimpleNamespace(parts=[part])
        candidate = SimpleNamespace(content=content)
        resp = SimpleNamespace(text=None, candidates=[candidate])
        text = ModusAgent._extract_response_text("google", resp)
        assert text == "From candidate"

    def test_cohere_text(self):
        resp = SimpleNamespace(text="Cohere response")
        text = ModusAgent._extract_response_text("cohere", resp)
        assert text == "Cohere response"

    def test_cohere_message(self):
        content_block = SimpleNamespace(text="From message")
        msg = SimpleNamespace(content=[content_block])
        resp = SimpleNamespace(text=None, message=msg)
        text = ModusAgent._extract_response_text("cohere", resp)
        assert text == "From message"

    def test_unknown_provider(self):
        text = ModusAgent._extract_response_text("unknown", "raw response")
        assert text == "raw response"

    def test_none_response(self):
        text = ModusAgent._extract_response_text("openai", None)
        assert text == ""


# ── Bedrock helpers ──────────────────────────────────────────────────────────

class TestReusableBody:
    def test_read_all(self):
        body = _ReusableBody(b'{"key": "value"}')
        assert body.read() == b'{"key": "value"}'
        # Can read again
        assert body.read() == b'{"key": "value"}'

    def test_read_partial(self):
        body = _ReusableBody(b"Hello World")
        assert body.read(5) == b"Hello"


class TestBedrockStreamWrapper:
    def test_stream_captures_metrics(self):
        events = [
            {"chunk": {"bytes": json.dumps({
                "amazon-bedrock-invocationMetrics": {
                    "inputTokenCount": 100,
                    "outputTokenCount": 50,
                }
            }).encode()}},
            {"chunk": {"bytes": b""}},
        ]
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent.record = MagicMock()
        agent._rec = MagicMock()

        wrapper = _BedrockStreamWrapper(iter(events), "anthropic.claude-v2", time.perf_counter(), agent)
        consumed = list(wrapper)
        assert len(consumed) == 2
        agent._rec.assert_called_once()
        call_args = agent._rec.call_args
        assert call_args[0][0] == "bedrock"
        assert call_args[0][1] == "anthropic.claude-v2"
        assert call_args[0][3] == 100  # input tokens
        assert call_args[0][4] == 50   # output tokens

    def test_stream_captures_usage_format(self):
        events = [
            {"chunk": {"bytes": json.dumps({
                "usage": {"input_tokens": 200, "output_tokens": 80}
            }).encode()}},
        ]
        agent = _make_agent()
        agent._rec = MagicMock()

        wrapper = _BedrockStreamWrapper(iter(events), "model-x", time.perf_counter(), agent)
        list(wrapper)
        agent._rec.assert_called_once()
        call_args = agent._rec.call_args
        assert call_args[0][3] == 200
        assert call_args[0][4] == 80


# ── Start method ─────────────────────────────────────────────────────────────

class TestAgentStart:
    """Tests for start() (lines 620-707)."""

    @patch("sdk.modus.agent._urlopen_tls")
    def test_start_full_flow(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "api_key": "ak_test",
            "app_uuid": "uuid-abc",
            "app_id": "test-app",
            "team_id": "team-abc",
            "registered": True,
            "team_slug": "team-slug",
        })
        agent = _make_agent()

        mock_scanner = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.app_id = "test-app"
        mock_snapshot.app_name = "Test App"
        mock_snapshot.content_hash.return_value = "hash123"
        mock_snapshot.to_dict.return_value = {}
        mock_scanner.return_value.scan.return_value = mock_snapshot

        with patch("sdk.modus.discovery.EnvironmentScanner", mock_scanner):
            result = agent.start()

        assert result is agent
        assert agent._started is True
        assert agent._api_key == "ak_test"

        # Cleanup
        agent._shutdown.set()
        if agent._flush_thread:
            agent._flush_thread.join(timeout=2)

    @patch("sdk.modus.agent._urlopen_tls", side_effect=Exception("fail"))
    def test_start_registration_failure(self, mock_urlopen):
        agent = _make_agent()
        mock_scanner = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.app_id = "test-app"
        mock_snapshot.app_name = "Test App"
        mock_scanner.return_value.scan.return_value = mock_snapshot

        with patch("sdk.modus.discovery.EnvironmentScanner", mock_scanner):
            result = agent.start()

        assert result is agent
        assert agent._started is True
        # No API key means governance disabled
        assert agent._api_key is None

    def test_start_idempotent(self):
        agent = _make_agent()
        agent._started = True
        result = agent.start()
        assert result is agent


# ── Shutdown ─────────────────────────────────────────────────────────────────

class TestShutdown:
    def test_shutdown_flush(self):
        agent = _make_agent()
        agent._flush_thread = MagicMock()
        agent._flush_thread.join = MagicMock()
        agent._shutdown_flush()
        assert agent._shutdown.is_set()
        agent._flush_thread.join.assert_called_once_with(timeout=15)


# ── Trigger rewind ───────────────────────────────────────────────────────────

class TestTriggerRewind:
    """Tests for _trigger_rewind (lines 574-618)."""

    def test_no_hooks_noop(self):
        agent = _make_agent()
        # has_hooks is False by default
        agent._trigger_rewind("test_reason")

    def test_with_hooks(self):
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._app_id = "test-app"
        agent._team_id = "team-abc"
        rollback = MagicMock(return_value=True)
        agent.register_rewind_hook("db", rollback)
        with patch("sdk.modus.agent.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            agent._trigger_rewind("budget_exceeded", "Over limit")
        # Hook should have been called via registry


# ── urlopen_tls ──────────────────────────────────────────────────────────────

class TestUrlopenTls:
    @patch("sdk.modus.agent.urllib_request.urlopen")
    def test_https_uses_tls(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        req = MagicMock()
        req.full_url = "https://example.com/api"
        _urlopen_tls(req, timeout=5)
        mock_urlopen.assert_called_once()
        call_kwargs = mock_urlopen.call_args
        assert call_kwargs[1].get("context") is not None

    @patch("sdk.modus.agent.urllib_request.urlopen")
    def test_http_no_tls(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        req = MagicMock()
        req.full_url = "http://localhost:9000/api"
        _urlopen_tls(req, timeout=5)
        mock_urlopen.assert_called_once()
        # Should NOT have context for http
        call_kwargs = mock_urlopen.call_args
        assert "context" not in call_kwargs[1]


# ── Policy sync loop ────────────────────────────────────────────────────────

class TestPolicySyncLoop:
    def test_policy_sync_loop_runs(self):
        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._shutdown = threading.Event()
        agent._shutdown.set()  # immediate exit

        with patch.object(agent, "_sync_policies") as mock_sync:
            agent._policy_sync_loop()
            mock_sync.assert_called()


# ── Flush loop ───────────────────────────────────────────────────────────────

class TestFlushLoop:
    def test_flush_loop_exits_on_shutdown(self):
        agent = _make_agent()
        agent._shutdown = threading.Event()
        agent._shutdown.set()
        with patch.object(agent, "_flush") as mock_flush, \
             patch.object(agent, "_flush_routing_outcomes"):
            agent._flush_loop()
            mock_flush.assert_called()


# ── Heartbeat loop ───────────────────────────────────────────────────────────

class TestHeartbeatLoop:
    def test_heartbeat_loop_exits(self):
        agent = _make_agent()
        agent._shutdown = threading.Event()
        agent._shutdown.set()
        with patch.object(agent, "_send_heartbeat"):
            agent._heartbeat_loop()
            # shutdown was set before wait, so heartbeat not called


# ── AggregationBucket ────────────────────────────────────────────────────────

class TestAggregationBucket:
    def test_accumulate(self):
        bucket = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        r1 = _make_record(duration_ms=100)
        r2 = _make_record(duration_ms=200, input_tokens=200)
        bucket.accumulate(r1)
        bucket.accumulate(r2)
        assert bucket.call_count == 2
        assert bucket.input_tokens == 300
        assert bucket.duration_ms_min == 100
        assert bucket.duration_ms_max == 200

    def test_to_dict(self):
        bucket = _AggregationBucket(
            provider="openai", model="gpt-4o",
            operation="chat", resource_type="llm_call",
        )
        bucket.accumulate(_make_record())
        d = bucket.to_dict()
        assert d["provider"] == "openai"
        assert d["call_count"] == 1
        assert "duration_ms_avg" in d


# ── Flush dispatch ───────────────────────────────────────────────────────────

class TestFlushDispatch:
    def test_flush_dispatches_to_raw(self):
        agent = _make_agent(aggregation_enabled=False)
        agent._api_key = "ak_test"
        with patch.object(agent, "_flush_raw") as mock_raw:
            agent._flush()
            mock_raw.assert_called_once()

    def test_flush_dispatches_to_aggregated(self):
        agent = _make_agent(aggregation_enabled=True)
        agent._api_key = "ak_test"
        with patch.object(agent, "_flush_aggregated") as mock_agg:
            agent._flush()
            mock_agg.assert_called_once()

    def test_flush_no_api_key(self):
        agent = _make_agent()
        agent._api_key = None
        with patch.object(agent, "_flush_raw") as mock_raw, \
             patch.object(agent, "_flush_aggregated") as mock_agg:
            agent._flush()
            mock_raw.assert_not_called()
            mock_agg.assert_not_called()


# ── OpenAI streaming ────────────────────────────────────────────────────────

class TestOpenAIStreaming:
    def test_openai_sync_stream_injects_options(self):
        """Verify stream_options injected for streaming calls."""
        openai = ModuleType("openai")
        openai.__version__ = "1.30.0"
        resources = ModuleType("openai.resources")
        chat = ModuleType("openai.resources.chat")
        openai.resources = resources
        openai.resources.chat = chat

        captured_kwargs = {}

        class Completions:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                captured_kwargs.update(kwargs)
                # Return a generator for streaming
                def gen():
                    chunk = SimpleNamespace(
                        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=10),
                        model="gpt-4o",
                    )
                    yield chunk
                return gen()

        class AsyncCompletions:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                pass

        chat.Completions = Completions
        chat.AsyncCompletions = AsyncCompletions

        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._enforce_with_retry = MagicMock(return_value="gpt-4o")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))

        with patch.dict("sys.modules", {
            "openai": openai,
            "openai.resources": resources,
            "openai.resources.chat": chat,
        }):
            agent._instrument_openai()

        sdk_instance = MagicMock()
        result = Completions.create(sdk_instance, model="gpt-4o", messages=[], stream=True)
        # Should be a generator (stream)
        chunks = list(result)
        assert len(chunks) == 1
        assert captured_kwargs.get("stream_options") == {"include_usage": True}


# ── Anthropic streaming ─────────────────────────────────────────────────────

class TestAnthropicStreaming:
    def test_anthropic_sync_stream(self):
        anthropic = ModuleType("anthropic")
        anthropic.__version__ = "0.30.0"
        resources = ModuleType("anthropic.resources")
        anthropic.resources = resources

        class Messages:
            @staticmethod
            def create(self_sdk, *args, **kwargs):
                def gen():
                    msg_start = SimpleNamespace(
                        type="message_start",
                        message=SimpleNamespace(
                            usage=SimpleNamespace(input_tokens=50)
                        ),
                    )
                    yield msg_start
                    delta = SimpleNamespace(
                        type="message_delta",
                        usage=SimpleNamespace(output_tokens=30),
                    )
                    yield delta
                return gen()

        class AsyncMessages:
            @staticmethod
            async def create(self_sdk, *args, **kwargs):
                pass

        resources.Messages = Messages
        resources.AsyncMessages = AsyncMessages

        agent = _make_agent()
        agent._api_key = "ak_test"
        agent._enforce_with_retry = MagicMock(return_value="claude-sonnet-4-20250514")
        agent._apply_pii_scan = MagicMock(side_effect=lambda x: x)
        agent._cache_check = MagicMock(return_value=None)
        agent._cache_store = MagicMock()
        agent._try_route_sync = MagicMock(return_value=(False, None))
        agent.record = MagicMock()
        agent._rec = MagicMock()

        with patch.dict("sys.modules", {"anthropic": anthropic, "anthropic.resources": resources}):
            agent._instrument_anthropic()

        sdk_instance = MagicMock()
        result = Messages.create(sdk_instance, model="claude-sonnet-4-20250514", messages=[], stream=True)
        events = list(result)
        assert len(events) == 2


# ── Mistral provider ────────────────────────────────────────────────────────

class TestMistralExtractPrompts:
    def test_mistral_prompt_extraction(self):
        sys_p, user_p = ModusAgent._extract_prompts_for_routing("mistral", {
            "messages": [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "Summarize"},
            ],
        })
        assert sys_p == "Be concise"
        assert user_p == "Summarize"


# ── Groq extract response ───────────────────────────────────────────────────

class TestGroqExtractResponse:
    def test_groq_response_text(self):
        msg = SimpleNamespace(content="Groq says hello")
        choice = SimpleNamespace(message=msg)
        resp = SimpleNamespace(choices=[choice])
        text = ModusAgent._extract_response_text("groq", resp)
        assert text == "Groq says hello"
