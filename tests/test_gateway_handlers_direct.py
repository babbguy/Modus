"""
Gateway direct function tests

Tests gateway handler functions directly (bypassing the disabled router)
to cover the openai_chat_completions, openai_embeddings, anthropic_messages,
and streaming code paths.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fastapi import HTTPException


def _fake_app():
    app = MagicMock()
    app.id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    app.team_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app.app_id = "test-app"
    app.environment = "production"
    return app


def _make_request(body_bytes=b'{}', headers=None):
    request = MagicMock()
    request.body = AsyncMock(return_value=body_bytes)
    headers = headers or {}
    request.headers = MagicMock()
    request.headers.items.return_value = list(headers.items())
    request.headers.get = lambda k, default=None: headers.get(k, default)
    request.url = MagicMock()
    request.url.query = ""
    request.method = "POST"
    return request


# ── openai_chat_completions direct tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_chat_direct_success():
    from orchestrator.api.gateway import openai_chat_completions

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode(), {
        "authorization": "Bearer sk-test",
        "x-modus-apikey": "mds_test",
    })

    upstream_resp = httpx.Response(
        200,
        json={
            "id": "chatcmpl-test", "model": "gpt-4o",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        },
        headers={"content-type": "application/json"},
    )

    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
        patch("orchestrator.api.gateway._record_usage", new_callable=AsyncMock),
        patch("orchestrator.api.gateway._get_stream_budget_limit", new_callable=AsyncMock, return_value=0),
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await openai_chat_completions(request, mock_db)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_openai_chat_direct_policy_deny():
    from orchestrator.api.gateway import openai_chat_completions

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("deny", "Budget exceeded", None)),
    ):
        resp = await openai_chat_completions(request, mock_db)
    assert resp.status_code == 403
    data = json.loads(resp.body)
    assert data["error"]["code"] == "deny"


@pytest.mark.asyncio
async def test_openai_chat_direct_connect_error():
    from orchestrator.api.gateway import openai_chat_completions

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("fail"))
        mock_client.return_value = mock_http

        with pytest.raises(HTTPException) as exc_info:
            await openai_chat_completions(request, mock_db)
        assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_openai_chat_direct_timeout():
    from orchestrator.api.gateway import openai_chat_completions

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.return_value = mock_http

        with pytest.raises(HTTPException) as exc_info:
            await openai_chat_completions(request, mock_db)
        assert exc_info.value.status_code == 504


@pytest.mark.asyncio
async def test_openai_chat_direct_model_swap():
    from orchestrator.api.gateway import openai_chat_completions

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    upstream_resp = httpx.Response(
        200,
        json={"model": "gpt-4o-mini", "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}, "choices": []},
        headers={"content-type": "application/json"},
    )

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", "gpt-4o-mini")),
        patch("orchestrator.api.gateway._get_client") as mock_client,
        patch("orchestrator.api.gateway._record_usage", new_callable=AsyncMock),
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await openai_chat_completions(request, mock_db)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_openai_chat_direct_upstream_error():
    """Test non-200 response from upstream."""
    from orchestrator.api.gateway import openai_chat_completions

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    upstream_resp = httpx.Response(
        429,
        json={"error": {"message": "Rate limit exceeded"}},
        headers={"content-type": "application/json"},
    )

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
        patch("orchestrator.api.gateway._record_usage", new_callable=AsyncMock),
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await openai_chat_completions(request, mock_db)
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_openai_chat_direct_streaming_request():
    """Verify streaming request calls _stream_openai."""
    from orchestrator.api.gateway import openai_chat_completions

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_stream_budget_limit", new_callable=AsyncMock, return_value=0),
        patch("orchestrator.api.gateway._stream_openai", new_callable=AsyncMock, return_value=MagicMock(status_code=200)) as mock_stream,
    ):
        await openai_chat_completions(request, mock_db)
    mock_stream.assert_awaited_once()


# ── openai_embeddings direct tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_embeddings_direct_success():
    from orchestrator.api.gateway import openai_embeddings

    body = {"model": "text-embedding-3-small", "input": "Hello"}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    upstream_resp = httpx.Response(
        200,
        json={"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 5}},
        headers={"content-type": "application/json"},
    )

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
        patch("orchestrator.api.gateway._record_usage", new_callable=AsyncMock),
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await openai_embeddings(request, mock_db)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_openai_embeddings_direct_deny():
    from orchestrator.api.gateway import openai_embeddings

    body = {"model": "text-embedding-3-small", "input": "Hello"}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("deny", "blocked", None)),
    ):
        resp = await openai_embeddings(request, mock_db)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_openai_embeddings_direct_timeout():
    from orchestrator.api.gateway import openai_embeddings

    body = {"model": "text-embedding-3-small", "input": "Hello"}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
        mock_client.return_value = mock_http

        with pytest.raises(HTTPException) as exc_info:
            await openai_embeddings(request, mock_db)
        assert exc_info.value.status_code == 504


# ── anthropic_messages direct tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_messages_direct_success():
    from orchestrator.api.gateway import anthropic_messages

    body = {"model": "claude-sonnet-4-20250514", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    upstream_resp = httpx.Response(
        200,
        json={
            "id": "msg_test", "type": "message", "model": "claude-sonnet-4-20250514",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [{"type": "text", "text": "hello"}],
        },
        headers={"content-type": "application/json"},
    )

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
        patch("orchestrator.api.gateway._record_usage", new_callable=AsyncMock),
        patch("orchestrator.api.gateway._get_stream_budget_limit", new_callable=AsyncMock, return_value=0),
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await anthropic_messages(request, mock_db)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_anthropic_messages_direct_deny():
    from orchestrator.api.gateway import anthropic_messages

    body = {"model": "claude-sonnet-4-20250514", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("deny", "blocked", None)),
    ):
        resp = await anthropic_messages(request, mock_db)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_anthropic_messages_direct_connect_error():
    from orchestrator.api.gateway import anthropic_messages

    body = {"model": "claude-sonnet-4-20250514", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_client") as mock_client,
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("fail"))
        mock_client.return_value = mock_http

        with pytest.raises(HTTPException) as exc_info:
            await anthropic_messages(request, mock_db)
        assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_anthropic_messages_direct_model_swap():
    from orchestrator.api.gateway import anthropic_messages

    body = {"model": "claude-sonnet-4-20250514", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    upstream_resp = httpx.Response(
        200,
        json={"id": "msg_test", "type": "message", "model": "claude-haiku-4-5-20251001",
              "usage": {"input_tokens": 10, "output_tokens": 5}, "content": []},
        headers={"content-type": "application/json"},
    )

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", "claude-haiku-4-5-20251001")),
        patch("orchestrator.api.gateway._get_client") as mock_client,
        patch("orchestrator.api.gateway._record_usage", new_callable=AsyncMock),
    ):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=upstream_resp)
        mock_client.return_value = mock_http

        resp = await anthropic_messages(request, mock_db)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_anthropic_messages_direct_streaming():
    from orchestrator.api.gateway import anthropic_messages

    body = {"model": "claude-sonnet-4-20250514", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}], "stream": True}
    request = _make_request(json.dumps(body).encode())
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._evaluate_policy", new_callable=AsyncMock, return_value=("allow", "ok", None)),
        patch("orchestrator.api.gateway._get_stream_budget_limit", new_callable=AsyncMock, return_value=0),
        patch("orchestrator.api.gateway._stream_anthropic", new_callable=AsyncMock, return_value=MagicMock(status_code=200)) as mock_stream,
    ):
        await anthropic_messages(request, mock_db)
    mock_stream.assert_awaited_once()


# ── Passthrough direct tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_passthrough_direct():
    from orchestrator.api.gateway import openai_passthrough

    request = _make_request(b"", {"x-modus-apikey": "mds_test"})
    request.method = "GET"
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._passthrough", new_callable=AsyncMock, return_value=MagicMock(status_code=200)) as mock_pt,
    ):
        await openai_passthrough("v1/models", request, mock_db)
    mock_pt.assert_awaited_once()


@pytest.mark.asyncio
async def test_anthropic_passthrough_direct():
    from orchestrator.api.gateway import anthropic_passthrough

    request = _make_request(b"", {"x-modus-apikey": "mds_test"})
    request.method = "GET"
    mock_db = AsyncMock()

    with (
        patch("orchestrator.api.gateway._resolve_app", new_callable=AsyncMock, return_value=_fake_app()),
        patch("orchestrator.api.gateway._passthrough", new_callable=AsyncMock, return_value=MagicMock(status_code=200)) as mock_pt,
    ):
        await anthropic_passthrough("v1/models", request, mock_db)
    mock_pt.assert_awaited_once()


# ── _get_client ──────────────────────────────────────────────────────────────


def test_get_client_creates_new():
    import orchestrator.api.gateway as gw

    old = gw._client
    gw._client = None
    try:
        client = gw._get_client()
        assert client is not None
        assert not client.is_closed
    finally:
        # Clean up. asyncio.run creates and tears down its own event loop, which
        # is compatible with Python 3.13 (where asyncio.get_event_loop() no longer
        # implicitly creates a loop in a thread that has none).
        if gw._client and not gw._client.is_closed:
            asyncio.run(gw._client.aclose())
        gw._client = old


def test_get_client_returns_existing():
    import orchestrator.api.gateway as gw

    mock_client = MagicMock()
    mock_client.is_closed = False
    old = gw._client
    gw._client = mock_client
    try:
        result = gw._get_client()
        assert result is mock_client
    finally:
        gw._client = old
