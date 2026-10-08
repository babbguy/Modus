"""
Money-handling fixes — regression tests.

Covers:
  M1  — Guillotine budget limit is Decimal end-to-end (never float), and the
        limit variable is dollars (budget_limit_usd).
  M3  — A pricing error inside the mid-stream Guillotine path is logged with
        context and never silently swallowed; the stream stays alive.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Test doubles ──────────────────────────────────────────────────────────────


def _fake_app():
    app = MagicMock()
    app.id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    app.team_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app.environment = "production"
    return app


class _FakeStreamResp:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _fake_client_with_lines(lines, status_code=200):
    client = MagicMock()
    client.stream = MagicMock(return_value=_FakeStreamCtx(_FakeStreamResp(lines, status_code)))
    return client


async def _collect(streaming_response) -> str:
    chunks = []
    async for c in streaming_response.body_iterator:
        chunks.append(c if isinstance(c, str) else c.decode())
    # let the fire-and-forget _record_usage task settle
    await asyncio.sleep(0)
    return "".join(chunks)


# ── M1: budget limit is Decimal end-to-end ───────────────────────────────────


@pytest.mark.asyncio
async def test_get_stream_budget_limit_returns_decimal_default():
    from orchestrator.api import gateway as gw

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    limit = await gw._get_stream_budget_limit(_fake_app(), mock_db)
    assert isinstance(limit, Decimal)
    assert limit == Decimal(0)


@pytest.mark.asyncio
async def test_get_stream_budget_limit_returns_decimal_from_threshold():
    from orchestrator.api import gateway as gw

    threshold = MagicMock()
    threshold.critical_value = Decimal("2.50")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = threshold
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    limit = await gw._get_stream_budget_limit(_fake_app(), mock_db)
    assert isinstance(limit, Decimal)
    assert limit == Decimal("2.50")


@pytest.mark.asyncio
async def test_stream_openai_guillotine_fires_with_decimal_budget(monkeypatch):
    """A Decimal budget compared against a Decimal running cost fires the cut.

    If the comparison still used float(running_cost) >= Decimal budget it would
    raise TypeError — so a clean fire proves the path is Decimal end-to-end.
    """
    from orchestrator.api import gateway as gw
    import orchestrator.core.pricing as pricing

    lines = [
        'data: {"choices":[{"delta":{"content":"aaaa bbbb cccc dddd"},"index":0}]}',
        'data: [DONE]',
    ]
    monkeypatch.setattr(gw, "_get_client", lambda: _fake_client_with_lines(lines))
    monkeypatch.setattr(
        pricing, "estimate_cost",
        lambda *a, **k: (Decimal("0"), Decimal("0"), Decimal("5.00")),
    )
    monkeypatch.setattr(gw, "_record_usage", AsyncMock())

    resp = await gw._stream_openai(
        _fake_app(), "http://upstream", {}, b"{}", "gpt-4o", {},
        time.perf_counter(), budget_limit_usd=Decimal("1.00"),
    )
    out = await _collect(resp)
    assert "response truncated" in out
    assert "modus_budget_limit" in out
    assert "[DONE]" in out


@pytest.mark.asyncio
async def test_stream_openai_no_fire_below_budget(monkeypatch):
    from orchestrator.api import gateway as gw
    import orchestrator.core.pricing as pricing

    lines = [
        'data: {"choices":[{"delta":{"content":"hello"},"index":0}]}',
        'data: [DONE]',
    ]
    monkeypatch.setattr(gw, "_get_client", lambda: _fake_client_with_lines(lines))
    monkeypatch.setattr(
        pricing, "estimate_cost",
        lambda *a, **k: (Decimal("0"), Decimal("0"), Decimal("0.10")),
    )
    monkeypatch.setattr(gw, "_record_usage", AsyncMock())

    resp = await gw._stream_openai(
        _fake_app(), "http://upstream", {}, b"{}", "gpt-4o", {},
        time.perf_counter(), budget_limit_usd=Decimal("1.00"),
    )
    out = await _collect(resp)
    assert "response truncated" not in out


@pytest.mark.asyncio
async def test_stream_anthropic_guillotine_fires_with_decimal_budget(monkeypatch):
    from orchestrator.api import gateway as gw
    import orchestrator.core.pricing as pricing

    lines = [
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"aaaa bbbb cccc"}}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(gw, "_get_client", lambda: _fake_client_with_lines(lines))
    monkeypatch.setattr(
        pricing, "estimate_cost",
        lambda *a, **k: (Decimal("0"), Decimal("0"), Decimal("5.00")),
    )
    monkeypatch.setattr(gw, "_record_usage", AsyncMock())

    resp = await gw._stream_anthropic(
        _fake_app(), "http://upstream", {}, b"{}", "claude-sonnet-4-20250514", {},
        time.perf_counter(), budget_limit_usd=Decimal("1.00"),
    )
    out = await _collect(resp)
    assert "response truncated" in out
    assert "modus_budget_limit" in out


# ── M3: pricing error is logged, never silently swallowed ─────────────────────


@pytest.mark.asyncio
async def test_stream_openai_pricing_error_logs_and_keeps_stream_alive(monkeypatch, caplog):
    from orchestrator.api import gateway as gw
    import orchestrator.core.pricing as pricing

    lines = [
        'data: {"choices":[{"delta":{"content":"aaaa bbbb"},"index":0}]}',
        'data: [DONE]',
    ]
    monkeypatch.setattr(gw, "_get_client", lambda: _fake_client_with_lines(lines))

    def _boom(*a, **k):
        raise RuntimeError("pricing table missing")

    monkeypatch.setattr(pricing, "estimate_cost", _boom)
    monkeypatch.setattr(gw, "_record_usage", AsyncMock())

    with caplog.at_level(logging.WARNING):
        resp = await gw._stream_openai(
            _fake_app(), "http://upstream", {}, b"{}", "gpt-4o", {},
            time.perf_counter(), budget_limit_usd=Decimal("0.01"),
        )
        out = await _collect(resp)

    # Stream stayed alive — the real content line was still forwarded
    assert "[DONE]" in out
    # Guillotine did NOT silently fire on the pricing error
    assert "response truncated" not in out
    # Failure is visible, with context
    assert "Guillotine cost estimation failed" in caplog.text
    assert "provider=openai" in caplog.text


@pytest.mark.asyncio
async def test_stream_anthropic_pricing_error_logs_and_keeps_stream_alive(monkeypatch, caplog):
    from orchestrator.api import gateway as gw
    import orchestrator.core.pricing as pricing

    lines = [
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"aaaa bbbb"}}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(gw, "_get_client", lambda: _fake_client_with_lines(lines))

    def _boom(*a, **k):
        raise RuntimeError("pricing table missing")

    monkeypatch.setattr(pricing, "estimate_cost", _boom)
    monkeypatch.setattr(gw, "_record_usage", AsyncMock())

    with caplog.at_level(logging.WARNING):
        resp = await gw._stream_anthropic(
            _fake_app(), "http://upstream", {}, b"{}", "claude-sonnet-4-20250514", {},
            time.perf_counter(), budget_limit_usd=Decimal("0.01"),
        )
        out = await _collect(resp)

    assert "response truncated" not in out
    assert "Guillotine cost estimation failed" in caplog.text
    assert "provider=anthropic" in caplog.text
