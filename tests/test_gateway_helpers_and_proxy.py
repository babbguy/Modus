"""
Gateway proxy coverage.

Targets orchestrator.api.gateway: helper functions, token estimation,
header handling, error formatters, and proxy endpoints with mocked httpx.
"""
from __future__ import annotations

import json

# ═══════════════════════════════════════════════════════════════════════════════
# 1. TOKEN ESTIMATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_estimate_input_tokens_messages():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"messages": [{"content": "Hello world this is a test"}]}
    result = _estimate_input_tokens(body)
    assert result >= 5


def test_estimate_input_tokens_empty_messages():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"messages": []}
    # Falls through to input check
    result = _estimate_input_tokens(body)
    assert result >= 10  # default minimum


def test_estimate_input_tokens_string_input():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"input": "A very long string to embed for testing purposes"}
    result = _estimate_input_tokens(body)
    assert result >= 10


def test_estimate_input_tokens_list_input():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"input": ["first string", "second string"]}
    result = _estimate_input_tokens(body)
    assert result >= 5


def test_estimate_input_tokens_no_content():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {}
    result = _estimate_input_tokens(body)
    assert result >= 10  # minimum token count


def test_estimate_output_tokens():
    from orchestrator.api.gateway import _estimate_output_tokens
    assert _estimate_output_tokens({"max_tokens": 500}) == 500
    assert _estimate_output_tokens({"max_completion_tokens": 800}) == 800
    assert _estimate_output_tokens({}) == 1000


# ═══════════════════════════════════════════════════════════════════════════════
# 2. HEADER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def test_forward_headers():
    from orchestrator.api.gateway import _forward_headers
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": [
            (b"authorization", b"Bearer sk-test"),
            (b"content-type", b"application/json"),
            (b"host", b"example.com"),
            (b"x-modus-apikey", b"mds_abc123"),
            (b"helicone-auth", b"Bearer test"),
            (b"x-modus-session-id", b"sess-123"),
            (b"x-custom-header", b"value"),
        ],
        "query_string": b"",
        "root_path": "",
    }
    from starlette.requests import Request
    request = Request(scope)
    result = _forward_headers(request)
    # host, x-modus-apikey, helicone-auth, x-modus-session-id should be stripped
    assert "host" not in result
    assert "x-modus-apikey" not in result
    assert "helicone-auth" not in result
    assert "x-modus-session-id" not in result
    # Authorization and custom headers should be kept
    assert result["authorization"] == "Bearer sk-test"
    assert result["x-custom-header"] == "value"


def test_clean_response_headers():
    from orchestrator.api.gateway import _clean_response_headers
    import httpx
    headers = httpx.Headers({
        "content-type": "application/json",
        "transfer-encoding": "chunked",
        "content-encoding": "gzip",
        "content-length": "123",
        "x-request-id": "abc",
    })
    result = _clean_response_headers(headers)
    assert "transfer-encoding" not in result
    assert "content-encoding" not in result
    assert "content-length" not in result
    assert result["x-request-id"] == "abc"
    assert result["content-type"] == "application/json"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. SPAN META EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def test_extract_span_meta():
    from orchestrator.api.gateway import _extract_span_meta
    from starlette.requests import Request
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": [
            (b"x-modus-session-id", b"sess-abc"),
            (b"x-modus-span-name", b"my-span"),
            (b"x-modus-parent-id", b"parent-123"),
            (b"x-modus-call-id", b"call-456"),
        ],
        "query_string": b"",
        "root_path": "",
    }
    request = Request(scope)
    meta = _extract_span_meta(request)
    assert meta["source"] == "gateway"
    assert meta["mds_session_id"] == "sess-abc"
    assert meta["mds_span_name"] == "my-span"
    assert meta["mds_parent_id"] == "parent-123"
    assert meta["mds_call_id"] == "call-456"


def test_extract_span_meta_auto_call_id():
    from orchestrator.api.gateway import _extract_span_meta
    from starlette.requests import Request
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": [],
        "query_string": b"",
        "root_path": "",
    }
    request = Request(scope)
    meta = _extract_span_meta(request)
    assert meta["mds_call_id"].startswith("gw_")
    assert "mds_session_id" not in meta


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ERROR FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════

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
    resp = _anthropic_error("deny", "Not allowed")
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["type"] == "error"
    assert "Not allowed" in body["error"]["message"]


def test_anthropic_error_throttle():
    from orchestrator.api.gateway import _anthropic_error
    resp = _anthropic_error("throttle", "Slow down")
    assert resp.status_code == 429


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CLIENT LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════

async def test_close_gateway_client():
    from orchestrator.api import gateway
    # Ensure a client exists
    gateway._client = None
    c = gateway._get_client()
    assert c is not None
    assert not c.is_closed
    await gateway.close_gateway_client()
    assert gateway._client is None


async def test_get_client_reuses():
    from orchestrator.api import gateway
    gateway._client = None
    c1 = gateway._get_client()
    c2 = gateway._get_client()
    assert c1 is c2
    await gateway.close_gateway_client()


async def test_get_client_recreates_if_closed():
    from orchestrator.api import gateway
    gateway._client = None
    c1 = gateway._get_client()
    await c1.aclose()
    c2 = gateway._get_client()
    assert c2 is not c1
    assert not c2.is_closed
    await gateway.close_gateway_client()
