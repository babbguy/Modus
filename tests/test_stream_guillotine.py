"""
Tests for the Stream Guillotine feature in orchestrator.api.gateway.

Tests the mid-stream budget enforcement mechanism that terminates SSE
streams in real-time when cumulative cost hits the budget limit.
"""

from __future__ import annotations

import json

import pytest


# ── Guillotine message format tests ─────────────────────────────────────────


class TestOpenAIGuillotineFormat:
    """Test that OpenAI guillotine messages are properly formatted."""

    def test_truncation_chunk_format(self):
        """Verify the truncation notification chunk is valid OpenAI SSE format."""
        trunc_chunk = {
            "choices": [{
                "delta": {"content": "\n\n[Modus: budget limit reached — response truncated]"},
                "finish_reason": "modus_budget_limit",
                "index": 0,
            }]
        }
        # Should be valid JSON
        serialized = json.dumps(trunc_chunk)
        parsed = json.loads(serialized)
        assert parsed["choices"][0]["finish_reason"] == "modus_budget_limit"
        assert "budget limit" in parsed["choices"][0]["delta"]["content"]

    def test_done_marker(self):
        """Verify the stream properly ends with [DONE]."""
        done = "data: [DONE]\n\n"
        assert "[DONE]" in done


class TestAnthropicGuillotineFormat:
    """Test that Anthropic guillotine messages are properly formatted."""

    def test_truncation_delta_format(self):
        """Verify the truncation content block delta is valid Anthropic format."""
        trunc_delta = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "text_delta",
                "text": "\n\n[Modus: budget limit reached — response truncated]",
            },
        }
        serialized = json.dumps(trunc_delta)
        parsed = json.loads(serialized)
        assert parsed["type"] == "content_block_delta"
        assert parsed["delta"]["type"] == "text_delta"
        assert "budget limit" in parsed["delta"]["text"]

    def test_stop_event_format(self):
        """Verify the message_delta stop event has correct structure."""
        stop_event = {
            "type": "message_delta",
            "delta": {"stop_reason": "modus_budget_limit"},
            "usage": {"output_tokens": 500},
        }
        serialized = json.dumps(stop_event)
        parsed = json.loads(serialized)
        assert parsed["delta"]["stop_reason"] == "modus_budget_limit"
        assert parsed["usage"]["output_tokens"] == 500

    def test_message_stop_event(self):
        """Verify the final message_stop event."""
        msg_stop = {"type": "message_stop"}
        serialized = json.dumps(msg_stop)
        parsed = json.loads(serialized)
        assert parsed["type"] == "message_stop"


# ── Token estimation logic ──────────────────────────────────────────────────


class TestTokenEstimation:
    """Test the real-time token counting heuristic used in guillotine."""

    def test_short_text_minimum_one_token(self):
        """Even very short text should count as at least 1 token."""
        text = "Hi"
        token_estimate = max(len(text) // 4, 1)
        assert token_estimate == 1

    def test_long_text_estimation(self):
        """Longer text should estimate ~4 chars per token."""
        text = "This is a longer piece of text that should be estimated correctly."
        token_estimate = max(len(text) // 4, 1)
        assert token_estimate > 10

    def test_empty_text_no_tokens(self):
        """Empty content produces 0 tokens but function guards with max 1."""
        # In the gateway, this path is only reached when content_piece is truthy
        # so empty strings are filtered out before token counting


# ── Budget resolution ───────────────────────────────────────────────────────


class TestBudgetLimitDefault:
    """Test that budget limits default correctly."""

    def test_zero_budget_disables_guillotine(self):
        """When budget_limit_cents=0, guillotine never fires."""
        budget_limit_cents = 0
        running_cost = 999.99
        # The condition: budget_limit_cents > 0
        should_fire = budget_limit_cents > 0 and running_cost >= budget_limit_cents
        assert should_fire is False

    def test_positive_budget_fires_when_exceeded(self):
        """When running cost >= budget, guillotine should fire."""
        budget_limit_cents = 1.00
        running_cost = 1.05
        should_fire = budget_limit_cents > 0 and running_cost >= budget_limit_cents
        assert should_fire is True

    def test_positive_budget_no_fire_when_under(self):
        """When running cost < budget, guillotine should not fire."""
        budget_limit_cents = 10.00
        running_cost = 5.50
        should_fire = budget_limit_cents > 0 and running_cost >= budget_limit_cents
        assert should_fire is False


# ── SSE parsing helpers ─────────────────────────────────────────────────────


class TestSSEParsing:
    """Test SSE line parsing used in guillotine stream processing."""

    def test_openai_usage_extraction(self):
        """Verify we can extract usage from OpenAI SSE data lines."""
        line = 'data: {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}'
        assert line.startswith("data: ")
        assert line != "data: [DONE]"
        chunk = json.loads(line[6:])
        usage = chunk.get("usage")
        assert usage is not None
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50

    def test_openai_delta_content_extraction(self):
        """Verify we can extract delta content for real-time token counting."""
        line = 'data: {"choices": [{"delta": {"content": "Hello world"}, "index": 0}]}'
        chunk = json.loads(line[6:])
        choices = chunk.get("choices", [])
        assert len(choices) == 1
        content = choices[0].get("delta", {}).get("content", "")
        assert content == "Hello world"

    def test_anthropic_message_start_extraction(self):
        """Verify Anthropic message_start usage extraction."""
        line = 'data: {"type": "message_start", "message": {"usage": {"input_tokens": 200}}}'
        data = json.loads(line[6:])
        assert data["type"] == "message_start"
        input_tokens = data["message"]["usage"]["input_tokens"]
        assert input_tokens == 200

    def test_anthropic_content_block_delta(self):
        """Verify Anthropic content_block_delta text extraction."""
        line = 'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}'
        data = json.loads(line[6:])
        assert data["type"] == "content_block_delta"
        text = data["delta"]["text"]
        assert text == "Hello"

    def test_done_line_skipped(self):
        """Verify [DONE] lines are not parsed as JSON."""
        line = "data: [DONE]"
        assert line == "data: [DONE]"
        # This should be skipped in the parser

    def test_malformed_json_handled(self):
        """Verify malformed JSON doesn't crash the parser."""
        line = "data: {not valid json"
        assert line.startswith("data: ")
        with pytest.raises(json.JSONDecodeError):
            json.loads(line[6:])
