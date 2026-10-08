"""
Gateway proxy (orchestrator/api/gateway.py)

Tests the OpenAI, Anthropic proxy handlers plus passthrough and helpers.
Mocks httpx outbound calls and the app-key verification layer.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ── Helpers ──────────────────────────────────────────────────────────────────


def _fake_app():
    app = MagicMock()
    app.id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    app.team_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app.app_id = "test-app"
    app.environment = "production"
    return app


def _openai_chat_response(model="gpt-4o", content="Hello"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


def _anthropic_messages_response(model="claude-sonnet-4-20250514", text="Hello"):
    return {
        "id": "msg_test",
        "type": "message",
        "model": model,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "content": [{"type": "text", "text": text}],
    }


GATEWAY_HEADERS = {
    "x-modus-apikey": "mds_test_key_fake",
    "authorization": "Bearer sk-test-key",
}


# ── Token estimation helpers ────────────────────────────────────────────────


def test_estimate_input_tokens_messages():
    from orchestrator.api.gateway import _estimate_input_tokens

    body = {"messages": [{"role": "user", "content": "Hello world"}]}
    result = _estimate_input_tokens(body)
    assert result >= 2  # ~11 chars / 4


def test_estimate_input_tokens_string_input():
    from orchestrator.api.gateway import _estimate_input_tokens

    body = {"input": "Hello world, this is a test sentence."}
    result = _estimate_input_tokens(body)
    assert result >= 8


def test_estimate_input_tokens_list_input():
    from orchestrator.api.gateway import _estimate_input_tokens

    body = {"input": ["Hello", "World"]}
    result = _estimate_input_tokens(body)
    assert result >= 2


def test_estimate_input_tokens_empty():
    from orchestrator.api.gateway import _estimate_input_tokens

    result = _estimate_input_tokens({})
    assert result >= 10  # fallback minimum


def test_estimate_output_tokens():
    from orchestrator.api.gateway import _estimate_output_tokens

    assert _estimate_output_tokens({"max_tokens": 500}) == 500
    assert _estimate_output_tokens({"max_completion_tokens": 300}) == 300
    assert _estimate_output_tokens({}) == 1000


# ── Header helpers ──────────────────────────────────────────────────────────


def test_forward_headers():
    from orchestrator.api.gateway import _forward_headers

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.items.return_value = [
        ("authorization", "Bearer sk-test"),
        ("content-type", "application/json"),
        ("host", "localhost"),
        ("x-modus-apikey", "mds_abc"),
        ("x-custom", "value"),
    ]
    result = _forward_headers(request)
    assert "authorization" in result
    assert "content-type" in result
    assert "x-custom" in result
    assert "host" not in result
    assert "x-modus-apikey" not in result


def test_clean_response_headers():
    from orchestrator.api.gateway import _clean_response_headers

    headers = httpx.Headers({
        "content-type": "application/json",
        "transfer-encoding": "chunked",
        "content-encoding": "gzip",
        "x-request-id": "abc123",
    })
    result = _clean_response_headers(headers)
    assert "x-request-id" in result
    assert "transfer-encoding" not in result
    assert "content-encoding" not in result


# ── Span metadata extraction ─────────────────────────────────────────────────


def test_extract_span_meta():
    from orchestrator.api.gateway import _extract_span_meta

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.get = lambda k, default=None: {
        "x-modus-session-id": "sess-123",
        "x-modus-span-name": "my-span",
        "x-modus-parent-id": "parent-456",
        "x-modus-call-id": "call-789",
    }.get(k, default)

    meta = _extract_span_meta(request)
    assert meta["source"] == "gateway"
    assert meta["mds_session_id"] == "sess-123"
    assert meta["mds_span_name"] == "my-span"
    assert meta["mds_parent_id"] == "parent-456"
    assert meta["mds_call_id"] == "call-789"


def test_extract_span_meta_no_headers():
    from orchestrator.api.gateway import _extract_span_meta

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.get = lambda k, default=None: None

    meta = _extract_span_meta(request)
    assert meta["source"] == "gateway"
    assert "mds_session_id" not in meta
    assert "mds_call_id" in meta  # auto-generated


# ── Error formatters ─────────────────────────────────────────────────────────


def test_openai_error_deny():
    from orchestrator.api.gateway import _openai_error

    resp = _openai_error("deny", "Budget exceeded")
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["error"]["code"] == "deny"
    assert "Budget exceeded" in body["error"]["message"]


def test_openai_error_throttle():
    from orchestrator.api.gateway import _openai_error

    resp = _openai_error("throttle", "Rate limited")
    assert resp.status_code == 429


def test_anthropic_error_deny():
    from orchestrator.api.gateway import _anthropic_error

    resp = _anthropic_error("deny", "Policy blocked")
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["type"] == "error"
    assert "Policy blocked" in body["error"]["message"]


def test_anthropic_error_throttle():
    from orchestrator.api.gateway import _anthropic_error

    resp = _anthropic_error("throttle", "Slow down")
    assert resp.status_code == 429


# ── Policy gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_policy_enforcement_disabled():
    from orchestrator.api.gateway import _evaluate_policy

    app = _fake_app()
    with patch("orchestrator.api.gateway.settings") as mock_settings:
        mock_settings.enforcement_enabled = False
        decision, reason, suggested = await _evaluate_policy(
            app, "openai", "gpt-4o", 100, 50, MagicMock()
        )
    assert decision == "allow"
    assert reason == "ok"


@pytest.mark.asyncio
async def test_evaluate_policy_allow():
    from orchestrator.api.gateway import _evaluate_policy

    app = _fake_app()
    mock_result = MagicMock()
    mock_result.allowed = True
    mock_result.decision = "allow"
    mock_result.reason = "ok"
    mock_result.suggested_model = None

    with (
        patch("orchestrator.api.gateway.settings") as mock_settings,
        patch("orchestrator.core.policy_engine.evaluate_policies", new_callable=AsyncMock, return_value=mock_result),
    ):
        mock_settings.enforcement_enabled = True
        mock_settings.enforcement_fail_open = True
        decision, reason, suggested = await _evaluate_policy(
            app, "openai", "gpt-4o", 100, 50, MagicMock()
        )
    assert decision == "allow"


@pytest.mark.asyncio
async def test_evaluate_policy_deny():
    from orchestrator.api.gateway import _evaluate_policy

    app = _fake_app()
    mock_result = MagicMock()
    mock_result.allowed = False
    mock_result.decision = "deny"
    mock_result.reason = "Budget exceeded"
    mock_result.suggested_model = None

    with (
        patch("orchestrator.api.gateway.settings") as mock_settings,
        patch("orchestrator.core.policy_engine.evaluate_policies", new_callable=AsyncMock, return_value=mock_result),
    ):
        mock_settings.enforcement_enabled = True
        decision, reason, suggested = await _evaluate_policy(
            app, "openai", "gpt-4o", 100, 50, MagicMock()
        )
    assert decision == "deny"
    assert reason == "Budget exceeded"


@pytest.mark.asyncio
async def test_evaluate_policy_error_fail_open():
    from orchestrator.api.gateway import _evaluate_policy

    app = _fake_app()

    with (
        patch("orchestrator.api.gateway.settings") as mock_settings,
        patch("orchestrator.core.policy_engine.evaluate_policies", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
    ):
        mock_settings.enforcement_enabled = True
        mock_settings.enforcement_fail_open = True
        decision, reason, _ = await _evaluate_policy(
            app, "openai", "gpt-4o", 100, 50, MagicMock()
        )
    assert decision == "allow"
    assert "fail-open" in reason


@pytest.mark.asyncio
async def test_evaluate_policy_error_fail_closed():
    from orchestrator.api.gateway import _evaluate_policy

    app = _fake_app()

    with (
        patch("orchestrator.api.gateway.settings") as mock_settings,
        patch("orchestrator.core.policy_engine.evaluate_policies", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
    ):
        mock_settings.enforcement_enabled = True
        mock_settings.enforcement_fail_open = False
        decision, reason, _ = await _evaluate_policy(
            app, "openai", "gpt-4o", 100, 50, MagicMock()
        )
    assert decision == "deny"


# ── _passthrough helper (direct call, gateway router not enabled in test) ────


@pytest.mark.asyncio
async def test_passthrough_success():
    from orchestrator.api.gateway import _passthrough

    upstream_resp = httpx.Response(
        200,
        json={"data": [{"id": "gpt-4o"}]},
        headers={"content-type": "application/json"},
    )

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.items.return_value = [("authorization", "Bearer sk-test")]
    request.body = AsyncMock(return_value=b"")
    request.method = "GET"
    request.url = MagicMock()
    request.url.query = ""

    with patch("orchestrator.api.gateway._get_client") as mock_client:
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await _passthrough(request, "https://api.openai.com/v1/models")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_passthrough_with_query_string():
    from orchestrator.api.gateway import _passthrough

    upstream_resp = httpx.Response(200, json={}, headers={"content-type": "application/json"})

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.items.return_value = []
    request.body = AsyncMock(return_value=b"")
    request.method = "GET"
    request.url = MagicMock()
    request.url.query = "limit=10"

    with patch("orchestrator.api.gateway._get_client") as mock_client:
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await _passthrough(request, "https://api.openai.com/v1/models")
    assert resp.status_code == 200
    # Verify the URL had query string appended
    call_args = mock_http.request.call_args
    assert "limit=10" in call_args.kwargs.get("url", "")


@pytest.mark.asyncio
async def test_passthrough_connect_error():
    from orchestrator.api.gateway import _passthrough
    from fastapi import HTTPException

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.items.return_value = []
    request.body = AsyncMock(return_value=b"")
    request.method = "GET"
    request.url = MagicMock()
    request.url.query = ""

    with patch("orchestrator.api.gateway._get_client") as mock_client:
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(side_effect=httpx.ConnectError("fail"))
        mock_client.return_value = mock_http

        with pytest.raises(HTTPException) as exc_info:
            await _passthrough(request, "https://api.openai.com/v1/models")
        assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_passthrough_timeout():
    from orchestrator.api.gateway import _passthrough
    from fastapi import HTTPException

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.items.return_value = []
    request.body = AsyncMock(return_value=b"")
    request.method = "GET"
    request.url = MagicMock()
    request.url.query = ""

    with patch("orchestrator.api.gateway._get_client") as mock_client:
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(side_effect=httpx.TimeoutException("t"))
        mock_client.return_value = mock_http

        with pytest.raises(HTTPException) as exc_info:
            await _passthrough(request, "https://api.openai.com/v1/models")
        assert exc_info.value.status_code == 504


# ── Resolve app helper ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_app_no_key():
    from orchestrator.api.gateway import _resolve_app
    from fastapi import HTTPException

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.get = lambda k, default="": ""

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_app(request, MagicMock())
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_app_helicone_header():
    from orchestrator.api.gateway import _resolve_app

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.get = lambda k, default="": {
        "x-modus-apikey": "",
        "helicone-auth": "Bearer mds_test_key",
    }.get(k, default)

    mock_db = MagicMock()
    with patch("orchestrator.api.ingest._verify_app_key", new_callable=AsyncMock, return_value=_fake_app()) as mock_verify:
        result = await _resolve_app(request, mock_db)
        mock_verify.assert_awaited_once_with("mds_test_key", mock_db)
        assert result.id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_resolve_app_modus_apikey():
    from orchestrator.api.gateway import _resolve_app

    request = MagicMock()
    request.headers = MagicMock()
    request.headers.get = lambda k, default="": {
        "x-modus-apikey": "mds_my_key",
        "helicone-auth": "",
    }.get(k, default)

    mock_db = MagicMock()
    with patch("orchestrator.api.ingest._verify_app_key", new_callable=AsyncMock, return_value=_fake_app()):
        result = await _resolve_app(request, mock_db)
        assert result.id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


# ── Stream budget limit ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_stream_budget_limit_no_threshold(db_session):
    from orchestrator.api.gateway import _get_stream_budget_limit

    app = _fake_app()
    result = await _get_stream_budget_limit(app, db_session)
    assert result == 0.0


# ── Client lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_gateway_client():
    from orchestrator.api.gateway import close_gateway_client
    import orchestrator.api.gateway as gw

    # Set up a mock client
    mock_client = AsyncMock()
    mock_client.is_closed = False
    gw._client = mock_client

    await close_gateway_client()
    mock_client.aclose.assert_awaited_once()
    assert gw._client is None


@pytest.mark.asyncio
async def test_close_gateway_client_already_closed():
    from orchestrator.api.gateway import close_gateway_client
    import orchestrator.api.gateway as gw

    gw._client = None  # Already None
    await close_gateway_client()  # Should not raise


# ── Record usage (fire-and-forget) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_success():
    from orchestrator.api.gateway import _record_usage

    app = _fake_app()
    with (
        patch("orchestrator.core.pricing.estimate_cost", return_value=(0.01, 0.005, 0.015)),
        patch("orchestrator.core.write_queue.enqueue", new_callable=AsyncMock),
    ):
        await _record_usage(app, "openai", "gpt-4o", 100, 50, 200, {"source": "gateway"})
        # Should not raise


@pytest.mark.asyncio
async def test_record_usage_error_suppressed():
    from orchestrator.api.gateway import _record_usage

    app = _fake_app()
    with patch("orchestrator.core.pricing.estimate_cost", side_effect=RuntimeError("boom")):
        await _record_usage(app, "openai", "gpt-4o", 100, 50, 200)
        # Should not raise — errors are logged and suppressed
