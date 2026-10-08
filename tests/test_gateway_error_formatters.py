"""Tests for orchestrator.api.gateway — gateway proxy helpers and error formatters."""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest

from orchestrator.db.models import App, Team


def _team(**kw) -> Team:
    defaults = {"id": str(uuid.uuid4()), "slug": "gw-team", "name": "GW Team"}
    defaults.update(kw)
    return Team(**defaults)


def _app(team_id: str, **kw) -> App:
    defaults = {
        "id": str(uuid.uuid4()),
        "team_id": team_id,
        "app_id": "gw-app",
        "app_name": "Gateway App",
        "environment": "production",
        "api_key_hash": "x",
        "api_key_prefix": "mds_test",
    }
    defaults.update(kw)
    return App(**defaults)


# ── Unit tests for helpers ───────────────────────────────────────────────────


def test_estimate_input_tokens_messages():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"messages": [{"content": "Hello world, this is a test"}]}
    tokens = _estimate_input_tokens(body)
    assert tokens >= 5


def test_estimate_input_tokens_string_input():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"input": "Hello world, this is an embedding input"}
    tokens = _estimate_input_tokens(body)
    assert tokens >= 5


def test_estimate_input_tokens_list_input():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"input": ["Hello", "World"]}
    tokens = _estimate_input_tokens(body)
    assert tokens >= 2


def test_estimate_input_tokens_empty():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {}
    tokens = _estimate_input_tokens(body)
    assert tokens == 10  # min(0//4, 10) fallback


def test_estimate_output_tokens():
    from orchestrator.api.gateway import _estimate_output_tokens
    assert _estimate_output_tokens({"max_tokens": 500}) == 500
    assert _estimate_output_tokens({"max_completion_tokens": 200}) == 200
    assert _estimate_output_tokens({}) == 1000  # default


def test_openai_error_format():
    from orchestrator.api.gateway import _openai_error
    resp = _openai_error("deny", "Budget exceeded")
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["error"]["type"] == "modus_policy_error"
    assert "Budget exceeded" in body["error"]["message"]


def test_openai_error_throttle():
    from orchestrator.api.gateway import _openai_error
    resp = _openai_error("throttle", "Rate limit")
    assert resp.status_code == 429


def test_anthropic_error_format():
    from orchestrator.api.gateway import _anthropic_error
    resp = _anthropic_error("deny", "Model blocked")
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["type"] == "error"
    assert "Model blocked" in body["error"]["message"]


def test_anthropic_error_throttle():
    from orchestrator.api.gateway import _anthropic_error
    resp = _anthropic_error("throttle", "Too fast")
    assert resp.status_code == 429


def test_forward_headers():
    from orchestrator.api.gateway import _forward_headers
    mock_req = MagicMock()
    mock_req.headers.items.return_value = [
        ("authorization", "Bearer sk-test"),
        ("content-type", "application/json"),
        ("host", "example.com"),
        ("x-modus-apikey", "mds_xxx"),
        ("x-custom", "keep"),
    ]
    result = _forward_headers(mock_req)
    assert "authorization" in result
    assert "x-custom" in result
    assert "host" not in result
    assert "x-modus-apikey" not in result


def test_extract_span_meta():
    from orchestrator.api.gateway import _extract_span_meta
    mock_req = MagicMock()
    mock_req.headers.get.side_effect = lambda k, d=None: {
        "x-modus-session-id": "sess-1",
        "x-modus-span-name": "my-span",
        "x-modus-parent-id": "parent-1",
        "x-modus-call-id": "call-1",
    }.get(k, d)
    meta = _extract_span_meta(mock_req)
    assert meta["mds_session_id"] == "sess-1"
    assert meta["mds_span_name"] == "my-span"
    assert meta["mds_parent_id"] == "parent-1"
    assert meta["mds_call_id"] == "call-1"
    assert meta["source"] == "gateway"


def test_extract_span_meta_no_headers():
    from orchestrator.api.gateway import _extract_span_meta
    mock_req = MagicMock()
    mock_req.headers.get.return_value = None
    meta = _extract_span_meta(mock_req)
    assert meta["source"] == "gateway"
    assert "mds_call_id" in meta  # auto-generated


def test_clean_response_headers():
    import httpx
    from orchestrator.api.gateway import _clean_response_headers
    headers = httpx.Headers({
        "content-type": "application/json",
        "transfer-encoding": "chunked",
        "x-request-id": "abc",
    })
    result = _clean_response_headers(headers)
    assert "content-type" in result
    assert "x-request-id" in result
    assert "transfer-encoding" not in result


# ── Gateway client lifecycle ─────────────────────────────────────────────────


def test_get_client_creates_singleton():
    from orchestrator.api.gateway import _get_client
    c = _get_client()
    assert c is not None
    assert not c.is_closed


@pytest.mark.asyncio
async def test_close_gateway_client():
    from orchestrator.api.gateway import _get_client, close_gateway_client
    _ = _get_client()
    await close_gateway_client()


# ── Gateway endpoint auth errors ─────────────────────────────────────────────
# Gateway is gated by settings.gateway_enabled (default=False).
# When disabled, gateway routes return 404. We verify the routes exist
# but are correctly gated.


@pytest.mark.asyncio
async def test_gateway_disabled_by_default(client):
    """Gateway endpoints return 404 when gateway_enabled is False (default)."""
    resp = await client.post(
        "/gateway/openai/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_gateway_anthropic_disabled(client):
    resp = await client.post(
        "/gateway/anthropic/v1/messages",
        json={"model": "claude-sonnet-4-5-20250514", "messages": []},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_gateway_passthrough_disabled(client):
    resp = await client.get("/gateway/openai/v1/models")
    assert resp.status_code == 404


# ── _resolve_app auth ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_app_no_key():
    """_resolve_app raises 401 when no API key header is present."""
    from orchestrator.api.gateway import _resolve_app
    mock_req = MagicMock()
    mock_req.headers.get.return_value = ""
    with pytest.raises(Exception) as exc_info:
        await _resolve_app(mock_req, MagicMock())
    assert "401" in str(exc_info.value.status_code)


# ── Stream budget limit resolution ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_stream_budget_limit_no_threshold(db_session):
    from orchestrator.api.gateway import _get_stream_budget_limit
    app = MagicMock()
    app.id = str(uuid.uuid4())
    result = await _get_stream_budget_limit(app, db_session)
    assert result == 0.0
