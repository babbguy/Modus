"""
Tests for orchestrator.api.gateway — Proxy handlers, error formatters,
header helpers, span metadata extraction, and policy gate.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from orchestrator.api.gateway import (
    _anthropic_error,
    _clean_response_headers,
    _estimate_input_tokens,
    _estimate_output_tokens,
    _extract_span_meta,
    _forward_headers,
    _get_client,
    _openai_error,
    close_gateway_client,
)
# ── Error formatters ─────────────────────────────────────────────────────────


class TestOpenAIError:
    def test_deny_returns_403(self):
        resp = _openai_error("deny", "Budget exceeded")
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["error"]["code"] == "deny"
        assert "Budget exceeded" in body["error"]["message"]

    def test_throttle_returns_429(self):
        resp = _openai_error("throttle", "Rate limited")
        assert resp.status_code == 429
        body = json.loads(resp.body)
        assert body["error"]["type"] == "modus_policy_error"

    def test_media_type(self):
        resp = _openai_error("deny", "test")
        assert resp.media_type == "application/json"


class TestAnthropicError:
    def test_deny_returns_403(self):
        resp = _anthropic_error("deny", "Not allowed")
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["type"] == "error"
        assert body["error"]["type"] == "modus_policy_error"
        assert "Not allowed" in body["error"]["message"]

    def test_throttle_returns_429(self):
        resp = _anthropic_error("throttle", "Slow down")
        assert resp.status_code == 429


# ── Header helpers ───────────────────────────────────────────────────────────


class TestForwardHeaders:
    def test_strips_modus_headers(self):
        mock_req = MagicMock()
        mock_req.headers = {
            "authorization": "Bearer sk-xxx",
            "content-type": "application/json",
            "x-modus-apikey": "mds_secret",
            "helicone-auth": "Bearer old-key",
            "x-modus-session-id": "sess-1",
            "x-custom": "keep-me",
        }
        result = _forward_headers(mock_req)
        assert "authorization" in result
        assert "content-type" in result
        assert "x-custom" in result
        assert "x-modus-apikey" not in result
        assert "helicone-auth" not in result
        assert "x-modus-session-id" not in result

    def test_strips_host_and_content_length(self):
        mock_req = MagicMock()
        mock_req.headers = {
            "host": "modus.example.com",
            "content-length": "1234",
            "authorization": "Bearer key",
        }
        result = _forward_headers(mock_req)
        assert "host" not in result
        assert "content-length" not in result
        assert "authorization" in result


class TestCleanResponseHeaders:
    def test_strips_encoding_headers(self):
        import httpx
        headers = httpx.Headers({
            "content-type": "application/json",
            "transfer-encoding": "chunked",
            "content-encoding": "gzip",
            "content-length": "1234",
            "x-request-id": "req-1",
        })
        result = _clean_response_headers(headers)
        assert "content-type" in result
        assert "x-request-id" in result
        assert "transfer-encoding" not in result
        assert "content-encoding" not in result
        assert "content-length" not in result


# ── Span metadata ────────────────────────────────────────────────────────────


class TestExtractSpanMeta:
    def test_basic_extraction(self):
        mock_req = MagicMock()
        mock_req.headers = MagicMock()
        mock_req.headers.get = lambda key, default=None: {
            "x-modus-session-id": "sess-123",
            "x-modus-span-name": "summarize",
            "x-modus-parent-id": "parent-1",
            "x-modus-call-id": "call-1",
        }.get(key, default)
        meta = _extract_span_meta(mock_req)
        assert meta["source"] == "gateway"
        assert meta["mds_session_id"] == "sess-123"
        assert meta["mds_span_name"] == "summarize"
        assert meta["mds_parent_id"] == "parent-1"
        assert meta["mds_call_id"] == "call-1"

    def test_missing_headers_auto_generates_call_id(self):
        mock_req = MagicMock()
        mock_req.headers = MagicMock()
        mock_req.headers.get = lambda key, default=None: None
        meta = _extract_span_meta(mock_req)
        assert meta["source"] == "gateway"
        assert meta["mds_call_id"].startswith("gw_")
        assert "mds_session_id" not in meta
        assert "mds_span_name" not in meta

    def test_partial_headers(self):
        mock_req = MagicMock()
        mock_req.headers = MagicMock()
        mock_req.headers.get = lambda key, default=None: {
            "x-modus-session-id": "s1",
        }.get(key, default)
        meta = _extract_span_meta(mock_req)
        assert meta["mds_session_id"] == "s1"
        assert meta["mds_call_id"].startswith("gw_")


# ── Token estimation edge cases ──────────────────────────────────────────────


class TestTokenEstimationEdgeCases:
    def test_messages_with_no_content(self):
        body = {"messages": [{"role": "system"}, {"role": "user"}]}
        tokens = _estimate_input_tokens(body)
        assert tokens == 10  # min floor

    def test_input_as_empty_list(self):
        body = {"input": []}
        tokens = _estimate_input_tokens(body)
        assert tokens == 10

    def test_output_prefers_max_tokens(self):
        body = {"max_tokens": 200, "max_completion_tokens": 300}
        assert _estimate_output_tokens(body) == 200

    def test_output_falls_back_to_completion_tokens(self):
        body = {"max_completion_tokens": 300}
        assert _estimate_output_tokens(body) == 300


# ── HTTP client management ───────────────────────────────────────────────────


class TestClientManagement:
    def test_get_client_returns_client(self):
        client = _get_client()
        assert client is not None
        assert not client.is_closed

    @pytest.mark.asyncio
    async def test_close_client(self):
        _get_client()  # ensure it's created
        await close_gateway_client()
        import orchestrator.api.gateway as gw_mod
        assert gw_mod._client is None

    @pytest.mark.asyncio
    async def test_close_client_idempotent(self):
        import orchestrator.api.gateway as gw_mod
        gw_mod._client = None
        await close_gateway_client()
        assert gw_mod._client is None


# ── Gateway API integration tests ────────────────────────────────────────────
# NOTE: Gateway routes are only registered when settings.gateway_enabled=True.
# In test mode this is False by default, so gateway endpoints return 404.
# We test that the routes are NOT registered when the feature is disabled.


async def test_gateway_disabled_returns_404(client):
    """Gateway routes should not be registered when gateway_enabled is False."""
    resp = await client.post(
        "/api/v1/gateway/openai/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404  # Not registered


async def test_gateway_anthropic_disabled(client):
    resp = await client.post(
        "/api/v1/gateway/anthropic/v1/messages",
        json={"model": "claude-3-haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404


async def test_gateway_embeddings_disabled(client):
    resp = await client.post(
        "/api/v1/gateway/openai/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "test"},
    )
    assert resp.status_code == 404
