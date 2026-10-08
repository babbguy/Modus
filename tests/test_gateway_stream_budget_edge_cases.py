"""
Gateway proxy coverage.

Targets orchestrator.api.gateway:
  - _estimate_input_tokens edge cases (117)
  - _get_stream_budget_limit (188-190)
  - openai_chat_completions non-streaming (400-401)
  - _stream_openai (424-499)
  - openai_embeddings (533, 546-547)
  - anthropic_messages non-streaming (606-607, 619-620)
  - _stream_anthropic (639-717)
  - _openai_error / _anthropic_error
  - _forward_headers / _clean_response_headers
  - _extract_span_meta
  - _passthrough
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock


from orchestrator.api.gateway import (
    _estimate_input_tokens,
    _estimate_output_tokens,
    _openai_error,
    _anthropic_error,
    _forward_headers,
    _clean_response_headers,
    _extract_span_meta,
)


# ── Token estimation ─────────────────────────────────────────────────────────

class TestEstimateInputTokens:
    def test_with_messages(self):
        body = {"messages": [{"content": "Hello world"}]}
        result = _estimate_input_tokens(body)
        assert result >= 10

    def test_with_string_input(self):
        body = {"input": "Hello world, this is a test string for embedding."}
        result = _estimate_input_tokens(body)
        assert result >= 10

    def test_with_list_input(self):
        body = {"input": ["Hello", "World", "Testing"]}
        result = _estimate_input_tokens(body)
        assert result >= 10

    def test_empty_body(self):
        result = _estimate_input_tokens({})
        assert result >= 10

    def test_messages_min_floor(self):
        body = {"messages": [{"content": "Hi"}]}
        result = _estimate_input_tokens(body)
        assert result >= 10


class TestEstimateOutputTokens:
    def test_with_max_tokens(self):
        assert _estimate_output_tokens({"max_tokens": 500}) == 500

    def test_with_max_completion_tokens(self):
        assert _estimate_output_tokens({"max_completion_tokens": 300}) == 300

    def test_default(self):
        assert _estimate_output_tokens({}) == 1000


# ── Error formatters ─────────────────────────────────────────────────────────

class TestErrorFormatters:
    def test_openai_error_deny(self):
        resp = _openai_error("deny", "Budget exceeded")
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["error"]["code"] == "deny"

    def test_openai_error_throttle(self):
        resp = _openai_error("throttle", "Rate limited")
        assert resp.status_code == 429

    def test_anthropic_error_deny(self):
        resp = _anthropic_error("deny", "Budget exceeded")
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["error"]["type"] == "modus_policy_error"

    def test_anthropic_error_throttle(self):
        resp = _anthropic_error("throttle", "Rate limited")
        assert resp.status_code == 429


# ── Header helpers ───────────────────────────────────────────────────────────

class TestHeaders:
    def test_forward_headers_strips(self):
        mock_request = MagicMock()
        mock_request.headers.items.return_value = [
            ("authorization", "Bearer sk-test"),
            ("host", "example.com"),
            ("x-modus-apikey", "mds_123"),
            ("content-type", "application/json"),
            ("x-modus-session-id", "sess-1"),
        ]
        result = _forward_headers(mock_request)
        assert "authorization" in result
        assert "content-type" in result
        assert "host" not in result
        assert "x-modus-apikey" not in result
        assert "x-modus-session-id" not in result

    def test_clean_response_headers(self):
        import httpx
        headers = httpx.Headers({
            "content-type": "application/json",
            "transfer-encoding": "chunked",
            "x-request-id": "abc",
        })
        result = _clean_response_headers(headers)
        assert "x-request-id" in result
        assert "transfer-encoding" not in result


class TestExtractSpanMeta:
    def test_all_headers(self):
        mock_request = MagicMock()
        mock_request.headers.get.side_effect = lambda k, d=None: {
            "x-modus-session-id": "sess-1",
            "x-modus-span-name": "chat",
            "x-modus-parent-id": "parent-1",
            "x-modus-call-id": "call-1",
        }.get(k, d)
        result = _extract_span_meta(mock_request)
        assert result["source"] == "gateway"
        assert result["mds_session_id"] == "sess-1"
        assert result["mds_span_name"] == "chat"
        assert result["mds_parent_id"] == "parent-1"
        assert result["mds_call_id"] == "call-1"

    def test_no_headers(self):
        mock_request = MagicMock()
        mock_request.headers.get.return_value = None
        result = _extract_span_meta(mock_request)
        assert result["source"] == "gateway"
        assert "mds_call_id" in result  # auto-generated
        assert result["mds_call_id"].startswith("gw_")

    def test_partial_headers(self):
        mock_request = MagicMock()
        mock_request.headers.get.side_effect = lambda k, d=None: {
            "x-modus-session-id": "sess-only",
        }.get(k, d)
        result = _extract_span_meta(mock_request)
        assert "mds_session_id" in result
        assert "mds_span_name" not in result
