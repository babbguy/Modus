"""
Tests for orchestrator.api.gateway — Token estimation, HTTP client management,
and schema validation.
"""
from __future__ import annotations

import pytest

from orchestrator.api.gateway import (
    _estimate_input_tokens,
    _estimate_output_tokens,
    _get_client,
    close_gateway_client,
)


# ── Token estimation ────────────────────────────────────────────────────────


class TestEstimateInputTokens:
    def test_messages_list(self):
        body = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello, how are you?"},
            ]
        }
        tokens = _estimate_input_tokens(body)
        assert tokens > 0
        # ~4 chars per token, rough estimate
        total_chars = len("You are helpful.") + len("Hello, how are you?")
        expected = max(total_chars // 4, 10)
        assert tokens == expected

    def test_empty_messages(self):
        tokens = _estimate_input_tokens({"messages": []})
        # Empty messages: total_chars=0, max(0//4, 10) = 10
        assert tokens == 10

    def test_string_input(self):
        body = {"input": "This is a test string for embeddings."}
        tokens = _estimate_input_tokens(body)
        expected = max(len("This is a test string for embeddings.") // 4, 10)
        assert tokens == expected

    def test_list_input(self):
        body = {"input": ["text one", "text two"]}
        tokens = _estimate_input_tokens(body)
        assert tokens > 0

    def test_no_messages_no_input(self):
        tokens = _estimate_input_tokens({})
        # body.get("messages", []) = [], falsy -> skip
        # body.get("input", "") = "", isinstance(str) -> max(0//4, 10) = 10
        assert tokens == 10

    def test_minimum_token_count(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        tokens = _estimate_input_tokens(body)
        assert tokens >= 10

    def test_long_content(self):
        body = {
            "messages": [
                {"role": "user", "content": "x" * 4000}
            ]
        }
        tokens = _estimate_input_tokens(body)
        assert tokens >= 1000


class TestEstimateOutputTokens:
    def test_explicit_max_tokens(self):
        assert _estimate_output_tokens({"max_tokens": 500}) == 500

    def test_max_completion_tokens(self):
        assert _estimate_output_tokens({"max_completion_tokens": 250}) == 250

    def test_default(self):
        assert _estimate_output_tokens({}) == 1000

    def test_max_tokens_takes_precedence(self):
        body = {"max_tokens": 100, "max_completion_tokens": 200}
        assert _estimate_output_tokens(body) == 100


# ── HTTP client management ──────────────────────────────────────────────────


class TestGetClient:
    def test_creates_client(self):
        import orchestrator.api.gateway as gw
        gw._client = None
        client = _get_client()
        assert client is not None
        assert not client.is_closed

    def test_reuses_existing_client(self):
        c1 = _get_client()
        c2 = _get_client()
        assert c1 is c2


class TestCloseGatewayClient:
    @pytest.mark.asyncio
    async def test_close_existing_client(self):
        import orchestrator.api.gateway as gw
        _get_client()  # ensure a client exists
        await close_gateway_client()
        assert gw._client is None

    @pytest.mark.asyncio
    async def test_close_when_none(self):
        import orchestrator.api.gateway as gw
        gw._client = None
        await close_gateway_client()  # should not raise
        assert gw._client is None
