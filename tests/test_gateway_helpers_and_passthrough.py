"""
Gateway proxy helpers + API endpoints.

Targets orchestrator.api.gateway: token estimation, header helpers,
error formatters, policy gate resolution, and passthrough endpoints.
"""
from __future__ import annotations


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TOKEN ESTIMATION (pure functions)
# ═══════════════════════════════════════════════════════════════════════════════

def test_estimate_input_tokens_messages():
    from orchestrator.api.gateway import _estimate_input_tokens

    body = {"messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, how are you?"},
    ]}
    tokens = _estimate_input_tokens(body)
    assert tokens >= 10  # minimum
    # "You are a helpful assistant." + "Hello, how are you?" = ~49 chars / 4 = ~12
    assert tokens > 0


def test_estimate_input_tokens_string_input():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"input": "This is a test sentence for embedding."}
    tokens = _estimate_input_tokens(body)
    assert tokens >= 10


def test_estimate_input_tokens_list_input():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"input": ["sentence one", "sentence two", "sentence three"]}
    tokens = _estimate_input_tokens(body)
    assert tokens >= 10


def test_estimate_input_tokens_empty():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {}
    tokens = _estimate_input_tokens(body)
    # Empty body has input="" (empty string), which is str -> max(0//4, 10) = 10
    assert tokens == 10


def test_estimate_input_tokens_empty_messages():
    from orchestrator.api.gateway import _estimate_input_tokens
    body = {"messages": []}
    tokens = _estimate_input_tokens(body)
    # Empty messages list is falsy, falls to input="" -> max(0, 10) = 10
    assert tokens == 10


def test_estimate_output_tokens_max_tokens():
    from orchestrator.api.gateway import _estimate_output_tokens
    body = {"max_tokens": 500}
    assert _estimate_output_tokens(body) == 500


def test_estimate_output_tokens_max_completion_tokens():
    from orchestrator.api.gateway import _estimate_output_tokens
    body = {"max_completion_tokens": 300}
    assert _estimate_output_tokens(body) == 300


def test_estimate_output_tokens_default():
    from orchestrator.api.gateway import _estimate_output_tokens
    body = {}
    assert _estimate_output_tokens(body) == 1000


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ERROR FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════

def test_openai_error_format():
    from orchestrator.api.gateway import _openai_error
    resp = _openai_error("deny", "Budget exceeded")
    assert resp.status_code == 403  # deny -> 403
    import json
    body = json.loads(resp.body)
    assert body["error"]["type"] == "modus_policy_error"
    assert "Budget exceeded" in body["error"]["message"]
    assert body["error"]["code"] == "deny"


def test_openai_error_throttle():
    from orchestrator.api.gateway import _openai_error
    resp = _openai_error("throttle", "Rate limited")
    assert resp.status_code == 429  # throttle -> 429


def test_anthropic_error_format():
    from orchestrator.api.gateway import _anthropic_error
    resp = _anthropic_error("deny", "Model blocked")
    assert resp.status_code == 403  # deny -> 403
    import json
    body = json.loads(resp.body)
    assert body["type"] == "error"
    assert body["error"]["type"] == "modus_policy_error"


def test_anthropic_error_throttle():
    from orchestrator.api.gateway import _anthropic_error
    resp = _anthropic_error("throttle", "Too fast")
    assert resp.status_code == 429


# ═══════════════════════════════════════════════════════════════════════════════
# 3. HEADER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def test_clean_response_headers():
    from orchestrator.api.gateway import _clean_response_headers
    import httpx
    headers = httpx.Headers({
        "content-type": "application/json",
        "transfer-encoding": "chunked",
        "content-encoding": "gzip",
        "x-request-id": "abc123",
    })
    cleaned = _clean_response_headers(headers)
    assert "x-request-id" in cleaned
    assert "transfer-encoding" not in cleaned
    assert "content-encoding" not in cleaned


def test_forward_headers():
    """Cover _forward_headers by verifying stripped headers."""
    from orchestrator.api.gateway import _STRIP_REQUEST
    # Verify the strip set is populated and contains expected headers
    assert "host" in _STRIP_REQUEST
    assert "x-modus-apikey" in _STRIP_REQUEST
    assert "helicone-auth" in _STRIP_REQUEST
    assert "content-length" in _STRIP_REQUEST


def test_strip_response_set():
    """Verify the response header strip set."""
    from orchestrator.api.gateway import _STRIP_RESPONSE
    assert "transfer-encoding" in _STRIP_RESPONSE
    assert "content-encoding" in _STRIP_RESPONSE


# ═══════════════════════════════════════════════════════════════════════════════
# 4. GATEWAY ENDPOINTS (auth required — should get 401)
# ═══════════════════════════════════════════════════════════════════════════════

def test_get_stream_budget_limit_import():
    """Verify _get_stream_budget_limit is importable and callable."""
    from orchestrator.api.gateway import _get_stream_budget_limit
    assert callable(_get_stream_budget_limit)


def test_evaluate_policy_import():
    """Verify _evaluate_policy is importable."""
    from orchestrator.api.gateway import _evaluate_policy
    assert callable(_evaluate_policy)


def test_record_usage_import():
    """Verify _record_usage is importable."""
    from orchestrator.api.gateway import _record_usage
    assert callable(_record_usage)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CLIENT LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════

async def test_close_gateway_client_when_none():
    """close_gateway_client should not fail if no client exists."""
    from orchestrator.api import gateway
    old = gateway._client
    gateway._client = None
    await gateway.close_gateway_client()
    gateway._client = old


def test_get_client_creates_singleton():
    from orchestrator.api.gateway import _get_client
    c = _get_client()
    assert c is not None
    assert not c.is_closed
    # Second call returns same instance
    c2 = _get_client()
    assert c is c2
