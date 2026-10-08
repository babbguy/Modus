"""
Gateway hardening (resilience, token estimation, body cap).

Proof criteria:
  1. Circuit breaker opens after N consecutive failures and fails fast; recovers
     after cooldown (deterministic clock).
  2. Bounded retries on transient transport errors only; a returned HTTP
     response is never retried.
  3. Token estimation counts multi-part (vision) content and tool schemas that
     were previously ignored.
  4. Body-size cap rejects oversized proxied bodies.
"""
from __future__ import annotations

import httpx
import pytest

from orchestrator.core.gateway_resilience import (
    CircuitBreaker, CircuitOpenError, call_with_resilience, stream_with_resilience,
)
from orchestrator.api.gateway import _estimate_input_tokens


# ── 1. Circuit breaker ────────────────────────────────────────────────────────

async def test_circuit_opens_after_threshold_and_fails_fast():
    t = {"now": 0.0}
    cb = CircuitBreaker(fail_threshold=3, cooldown_s=30.0, clock=lambda: t["now"])

    async def always_fail():
        raise httpx.ConnectError("down")

    # 3 failures (with retries disabled) trip the circuit.
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            await call_with_resilience("openai", always_fail, max_retries=0, breaker=cb)

    assert cb.is_open("openai") is True

    # Now it fails fast without calling the upstream at all.
    calls = {"n": 0}

    async def counted_fail():
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(CircuitOpenError):
        await call_with_resilience("openai", counted_fail, max_retries=0, breaker=cb)
    assert calls["n"] == 0, "open circuit must not invoke the upstream"


async def test_circuit_recovers_after_cooldown():
    t = {"now": 0.0}
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0, clock=lambda: t["now"])

    async def fail():
        raise httpx.TimeoutException("slow")

    for _ in range(2):
        with pytest.raises(httpx.TimeoutException):
            await call_with_resilience("anthropic", fail, max_retries=0, breaker=cb)
    assert cb.is_open("anthropic") is True

    # Advance past cooldown → half-open, a trial succeeds and closes it.
    t["now"] = 31.0

    async def ok():
        return httpx.Response(200)

    resp = await call_with_resilience("anthropic", ok, max_retries=0, breaker=cb)
    assert resp.status_code == 200
    assert cb.is_open("anthropic") is False


# ── 2. Retries ────────────────────────────────────────────────────────────────

async def test_retries_transient_then_succeeds():
    cb = CircuitBreaker(fail_threshold=10)
    attempts = {"n": 0}
    slept = []

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("transient")
        return httpx.Response(200)

    async def fake_sleep(s):
        slept.append(s)

    resp = await call_with_resilience(
        "openai", flaky, max_retries=3, breaker=cb, sleep=fake_sleep
    )
    assert resp.status_code == 200
    assert attempts["n"] == 3
    assert slept == [0.2, 0.4], "exponential backoff between retries"


async def test_http_response_is_never_retried():
    cb = CircuitBreaker()
    attempts = {"n": 0}

    async def server_error():
        attempts["n"] += 1
        return httpx.Response(500)  # provider's answer, not a transport failure

    resp = await call_with_resilience("openai", server_error, max_retries=3, breaker=cb)
    assert resp.status_code == 500
    assert attempts["n"] == 1, "a returned HTTP response must not be retried"


# ── 3. Token estimation ───────────────────────────────────────────────────────

def test_token_estimate_counts_multipart_and_tools():
    plain = {"messages": [{"role": "user", "content": "hello world"}]}
    base = _estimate_input_tokens(plain)

    # Multi-part content with text buried in parts + an image.
    multipart = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "describe this image in great detail please"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]}
    mp = _estimate_input_tokens(multipart)
    assert mp > base, "multi-part (vision) content must count more than a short string"

    # Tool schemas add to the input estimate.
    with_tools = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "Get the weather for a location",
            "parameters": {"type": "object", "properties": {"loc": {"type": "string"}}},
        }}],
    }
    assert _estimate_input_tokens(with_tools) > _estimate_input_tokens(
        {"messages": [{"role": "user", "content": "hi"}]}
    )


def test_old_behavior_would_have_undercounted_image():
    # The image part is a tiny JSON string but represents ~800 tokens; the
    # estimator must not treat it as ~a-few chars.
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "x"}},
    ]}]}
    assert _estimate_input_tokens(body) >= 700


# ── 4. Body-size cap ──────────────────────────────────────────────────────────

async def test_body_cap_rejects_oversized(monkeypatch):
    from fastapi import HTTPException
    from orchestrator.api import gateway
    from orchestrator.core.config import settings

    monkeypatch.setattr(settings, "gateway_max_body_bytes", 1000)

    class _Req:
        headers = {"content-length": "5000"}

        async def body(self):
            return b"x" * 5000

    with pytest.raises(HTTPException) as exc:
        await gateway._read_capped_body(_Req())
    assert exc.value.status_code == 413


async def test_body_cap_allows_normal(monkeypatch):
    from orchestrator.api import gateway
    from orchestrator.core.config import settings

    monkeypatch.setattr(settings, "gateway_max_body_bytes", 1_000_000)

    class _Req:
        headers = {}

        async def body(self):
            return b'{"model":"gpt-4o"}'

    out = await gateway._read_capped_body(_Req())
    assert out == b'{"model":"gpt-4o"}'


# ── 5. Streaming resilience ───────────────────────────────────────────────────
# The streaming proxy paths (_stream_openai/_stream_anthropic) previously opened
# the upstream connection directly, bypassing the circuit breaker entirely. These
# prove the establishment phase now shares the same breaker + bounded retries,
# while never retrying once the response is handed back (bytes may be flowing).


class _FakeStreamCM:
    """Minimal stand-in for httpx's client.stream(...) context manager."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        if self._exc is not None:
            raise self._exc
        return self._resp

    async def __aexit__(self, *exc_info):
        self.exited = True
        return False


async def test_stream_fails_fast_when_circuit_open():
    t = {"now": 0.0}
    cb = CircuitBreaker(fail_threshold=1, cooldown_s=30.0, clock=lambda: t["now"])
    cb.record_failure("openai")  # threshold=1 → opens immediately
    assert cb.is_open("openai") is True

    calls = {"n": 0}

    def opener():
        calls["n"] += 1
        return _FakeStreamCM(resp=httpx.Response(200))

    with pytest.raises(CircuitOpenError):
        async with stream_with_resilience("openai", opener, breaker=cb):
            pass
    assert calls["n"] == 0, "open circuit must not open an upstream stream"


async def test_stream_retries_transient_then_establishes():
    cb = CircuitBreaker(fail_threshold=10)
    attempts = {"n": 0}
    slept = []
    last = {}

    def opener():
        attempts["n"] += 1
        if attempts["n"] < 3:
            return _FakeStreamCM(exc=httpx.ConnectError("transient"))
        cm = _FakeStreamCM(resp=httpx.Response(200))
        last["cm"] = cm
        return cm

    async def fake_sleep(s):
        slept.append(s)

    async with stream_with_resilience(
        "openai", opener, max_retries=3, breaker=cb, sleep=fake_sleep
    ) as resp:
        assert resp.status_code == 200

    assert attempts["n"] == 3
    assert slept == [0.2, 0.4], "exponential backoff between establishment retries"
    assert last["cm"].exited is True, "established stream must be closed on exit"


async def test_stream_established_response_not_retried():
    # A non-200 upstream *response* is an answer, not a transport failure — the
    # connection established, so it's yielded as-is and success is recorded.
    cb = CircuitBreaker()
    attempts = {"n": 0}

    def opener():
        attempts["n"] += 1
        return _FakeStreamCM(resp=httpx.Response(429))

    async with stream_with_resilience("openai", opener, breaker=cb) as resp:
        assert resp.status_code == 429

    assert attempts["n"] == 1, "an established response must not be retried"
    assert cb.is_open("openai") is False


async def test_stream_connect_failure_feeds_breaker():
    t = {"now": 0.0}
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=30.0, clock=lambda: t["now"])

    async def fake_sleep(s):
        pass

    def opener():
        return _FakeStreamCM(exc=httpx.ConnectError("down"))

    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            async with stream_with_resilience(
                "anthropic", opener, max_retries=0, breaker=cb, sleep=fake_sleep
            ):
                pass

    assert cb.is_open("anthropic") is True, "stream connect failures must trip the breaker"
