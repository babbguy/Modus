"""
Modus — Gateway Proxy (Phase 6)
=======================================
Language-agnostic AI gateway. Any application (Node.js, Go, Ruby, Java, Rust)
can use full Modus governance by changing their AI provider BASE_URL.

Usage:
    # OpenAI SDK (any language)
    OPENAI_BASE_URL=https://modus.example.com/gateway/openai/v1

    # Anthropic SDK
    ANTHROPIC_BASE_URL=https://modus.example.com/gateway/anthropic

    # All requests must include X-Modus-APIKey header for app identification.
    # The original provider Authorization header is forwarded as-is.

Governed endpoints (policy evaluation + usage recording):
    POST /gateway/openai/v1/chat/completions
    POST /gateway/openai/v1/embeddings
    POST /gateway/anthropic/v1/messages

Passthrough endpoints (auth required, forwarded without governance):
    ANY /gateway/openai/{path}
    ANY /gateway/anthropic/{path}

Helicone migration:
    Accepts Helicone-Auth header as alias for X-Modus-APIKey.
    Teams migrating from Helicone change one env var and get full governance.

Attribution for non-Python apps:
    X-Modus-Session-ID  — session ID for attribution grouping
    X-Modus-Span-Name   — span name for call graph labeling
    X-Modus-Parent-ID   — parent call ID for graph edges
    X-Modus-Call-ID     — explicit call ID (auto-generated if omitted)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import settings
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gateway", tags=["gateway"])


# ── HTTP client (lazy singleton, closed in lifespan) ──────────────────────────

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
            follow_redirects=False,
        )
    return _client


async def close_gateway_client() -> None:
    """Call from lifespan shutdown."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ── Proxy observability + resilience helpers ─────────────────────────────────

def _gw_count(provider: str, endpoint: str, outcome: str) -> None:
    try:
        from orchestrator.metrics.prometheus import GATEWAY_REQUESTS_TOTAL
        GATEWAY_REQUESTS_TOTAL.labels(
            provider=provider, endpoint=endpoint, outcome=outcome
        ).inc()
    except Exception:
        pass  # metrics optional


def _gw_observe(provider: str, status: int, elapsed_s: float) -> None:
    try:
        from orchestrator.metrics.prometheus import (
            GATEWAY_UPSTREAM_DURATION_SECONDS, GATEWAY_UPSTREAM_STATUS_TOTAL,
        )
        GATEWAY_UPSTREAM_DURATION_SECONDS.labels(provider=provider).observe(elapsed_s)
        GATEWAY_UPSTREAM_STATUS_TOTAL.labels(
            provider=provider, status=str(status)
        ).inc()
    except Exception:
        pass


def _gw_guillotine(provider: str) -> None:
    try:
        from orchestrator.metrics.prometheus import GATEWAY_GUILLOTINE_FIRES_TOTAL
        GATEWAY_GUILLOTINE_FIRES_TOTAL.labels(provider=provider).inc()
    except Exception:
        pass


async def _upstream_post(
    provider: str, endpoint: str, url: str, headers: dict, body: bytes, start: float
):
    """POST to an upstream provider with circuit breaking, retries, and metrics.

    Maps failures to provider-shaped HTTP errors: 503 when the circuit is open,
    502 on connect failure, 504 on timeout.
    """
    from orchestrator.core.gateway_resilience import call_with_resilience, CircuitOpenError

    client = _get_client()
    try:
        resp = await call_with_resilience(
            provider, lambda: client.post(url, headers=headers, content=body)
        )
    except CircuitOpenError as exc:
        _gw_count(provider, endpoint, "error")
        raise HTTPException(
            503, f"Upstream {provider} temporarily unavailable (circuit open); "
                 f"retry in ~{int(exc.retry_after_s)}s.",
            headers={"Retry-After": str(int(exc.retry_after_s) or 1)},
        )
    except httpx.ConnectError:
        _gw_count(provider, endpoint, "error")
        raise HTTPException(502, "Failed to connect to upstream provider.")
    except httpx.TimeoutException:
        _gw_count(provider, endpoint, "error")
        raise HTTPException(504, "Upstream provider timed out.")

    _gw_observe(provider, resp.status_code, time.perf_counter() - start)
    _gw_count(provider, endpoint, "allow")
    return resp


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _resolve_app(request: Request, db: AsyncSession):
    """
    Authenticate via X-Modus-APIKey (or Helicone-Auth for migration).
    Returns app snapshot with id, team_id, etc.
    """
    from orchestrator.api.ingest import _verify_app_key

    raw_key = (
        request.headers.get("x-modus-apikey")
        or request.headers.get("helicone-auth", "").removeprefix("Bearer ").strip()
    )
    if not raw_key:
        raise HTTPException(401, "X-Modus-APIKey header required for gateway proxy.")

    return await _verify_app_key(raw_key, db)


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body, rejecting anything over the configured cap.

    Prevents a large body from being fully buffered into memory on a small VPS.
    Checks Content-Length first, then enforces the cap on the read bytes.
    """
    cap = settings.gateway_max_body_bytes
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > cap:
                raise HTTPException(413, f"Request body exceeds {cap} bytes.")
        except ValueError:
            pass
    body = await request.body()
    if len(body) > cap:
        raise HTTPException(413, f"Request body exceeds {cap} bytes.")
    return body


# ── Token estimation ─────────────────────────────────────────────────────────

# A vision image part costs far more than its JSON length suggests; charge a
# flat per-image token estimate so budgets are not silently undercounted.
_IMAGE_PART_TOKENS = 800


def _content_chars(content) -> int:
    """Character count for a message's content, handling multi-part lists.

    OpenAI/Anthropic multi-part content is a list of parts, e.g.
    ``[{"type":"text","text":...}, {"type":"image_url",...}]``. Summing
    ``str(content)`` (the old behaviour) under-counts text buried in parts and
    ignores images entirely, corrupting the pre-call budget and the Guillotine.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        chars = 0
        for part in content:
            if not isinstance(part, dict):
                chars += len(str(part))
                continue
            ptype = part.get("type", "")
            if ptype in ("text", "input_text"):
                chars += len(str(part.get("text", "")))
            elif ptype in ("image_url", "input_image", "image"):
                chars += _IMAGE_PART_TOKENS * 4  # counted back to tokens below
            else:
                chars += len(str(part))
        return chars
    return len(str(content or ""))


def _estimate_input_tokens(body: dict) -> int:
    """Token estimate from request body (~4 chars per token).

    Accounts for multi-part (vision) content and tool/function schemas, which
    contribute to the real prompt but were previously ignored.
    """
    messages = body.get("messages", [])
    if messages:
        total_chars = sum(_content_chars(m.get("content", "")) for m in messages)
        # Tool / function schemas are sent with every request and count toward
        # input tokens.
        tools = body.get("tools") or body.get("functions")
        if tools:
            total_chars += len(str(tools))
        return max(total_chars // 4, 10)
    inp = body.get("input", "")
    if isinstance(inp, str):
        return max(len(inp) // 4, 10)
    if isinstance(inp, list):
        return max(sum(_content_chars(i) for i in inp) // 4, 10)
    return 100


def _estimate_output_tokens(body: dict) -> int:
    """Estimate output tokens from max_tokens or default."""
    return body.get("max_tokens") or body.get("max_completion_tokens") or 1000


# ── Policy gate ──────────────────────────────────────────────────────────────

async def _evaluate_policy(
    app, provider: str, model: str,
    input_tokens: int, output_tokens: int,
    db: AsyncSession,
) -> tuple[str, str, Optional[str]]:
    """
    Quick policy evaluation. Returns (decision, reason, suggested_model).
    Reuses the same policy engine as the evaluate endpoint.
    """
    if not settings.enforcement_enabled:
        return "allow", "ok", None

    try:
        from orchestrator.core.pricing import estimate_cost
        _, _, est_cost = estimate_cost(provider, model, input_tokens, output_tokens)
        total_est = Decimal(str(est_cost))

        from orchestrator.core.policy_engine import evaluate_policies
        result = await evaluate_policies(
            db=db,
            app_id=str(app.id),
            team_id=str(app.team_id),
            provider=provider,
            model=model,
            environment=getattr(app, "environment", "production"),
            estimated_tokens=input_tokens + output_tokens,
            estimated_cost=total_est,
        )
        if not result.allowed:
            return result.decision, result.reason, result.suggested_model
        return "allow", "ok", result.suggested_model
    except Exception as exc:
        logger.warning("Gateway policy evaluation failed: %s", exc)
        if settings.enforcement_fail_open:
            return "allow", "policy-error-fail-open", None
        return "deny", "Policy evaluation error", None


# ── Stream Guillotine — budget limit resolution ─────────────────────────────

async def _get_stream_budget_limit(app, db: AsyncSession) -> Decimal:
    """
    Resolve the stream-level budget limit for Guillotine enforcement.

    Checks (in order):
    1. App-level stream_budget_limit (if set)
    2. Threshold with scope='app' and metric='cost' for this app
    3. Default: 0 (disabled — no mid-stream enforcement)

    Returns budget limit in US dollars (not cents), as a Decimal so the
    Guillotine comparison stays bank-grade (NUMERIC(18,8), never float).
    """
    # Check for app-level threshold. Stream Guillotine uses the critical_value
    # as the hard cap (warning_value would fire alerts but not stop the stream).
    from orchestrator.db.models import Threshold
    result = await db.execute(
        select(Threshold).where(
            Threshold.app_id == str(app.id),
            Threshold.metric == "cost",
            Threshold.is_active == True,  # noqa: E712
        ).order_by(Threshold.critical_value.asc()).limit(1)
    )
    threshold = result.scalar_one_or_none()
    if threshold:
        return Decimal(str(threshold.critical_value))
    return Decimal(0)


# ── Usage recording ──────────────────────────────────────────────────────────

async def _record_usage(
    app, provider: str, model: str,
    input_tokens: int, output_tokens: int,
    duration_ms: int, metadata: Optional[dict] = None,
) -> None:
    """Record usage via the write queue (non-blocking, fire-and-forget)."""
    try:
        from orchestrator.core.pricing import estimate_cost
        _, _, cost = estimate_cost(provider, model, input_tokens, output_tokens)

        from orchestrator.core.write_queue import enqueue, IngestItem

        batch_id = f"gw_{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc)
        session_id = (metadata or {}).get("mds_session_id")

        record = dict(
            app_id=str(app.id),
            team_id=str(app.team_id),
            provider=provider,
            resource_type="chat",
            model=model,
            operation="gateway_proxy",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_cost=None,
            output_cost=None,
            total_cost=Decimal(str(cost)),
            duration_ms=duration_ms,
            timestamp=now,
            metadata_=metadata,
            batch_id=batch_id,
            session_id=session_id,
        )

        await enqueue(IngestItem(
            app_id=str(app.id),
            team_id=str(app.team_id),
            batch_id=batch_id,
            records=[record],
            record_count=1,
            agent_version="gateway/1.0",
            sdk_versions=None,
            total_cost=Decimal(str(cost)),
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            total_duration_ms=duration_ms,
        ))
    except Exception as exc:
        logger.error("Gateway usage recording failed: %s", exc)


# ── Header helpers ────────────────────────────────────────────────────────────

_STRIP_REQUEST = {
    "host", "x-modus-apikey", "helicone-auth",
    "content-length", "transfer-encoding",
    "x-modus-session-id", "x-modus-span-name",
    "x-modus-parent-id", "x-modus-call-id",
}

_STRIP_RESPONSE = {
    "transfer-encoding", "content-encoding", "content-length",
}


def _forward_headers(request: Request) -> dict[str, str]:
    """Build headers to forward to the upstream provider."""
    return {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQUEST
    }


def _clean_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Clean upstream response headers for forwarding back to client."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _STRIP_RESPONSE
    }


# ── Span metadata from headers (attribution for non-Python apps) ─────────────

def _extract_span_meta(request: Request) -> dict:
    """Extract attribution span metadata from gateway-specific headers."""
    meta = {"source": "gateway"}
    session_id = request.headers.get("x-modus-session-id")
    if session_id:
        meta["mds_session_id"] = session_id
    span_name = request.headers.get("x-modus-span-name")
    if span_name:
        meta["mds_span_name"] = span_name
    parent_id = request.headers.get("x-modus-parent-id")
    if parent_id:
        meta["mds_parent_id"] = parent_id
    call_id = request.headers.get("x-modus-call-id") or f"gw_{uuid.uuid4().hex[:12]}"
    meta["mds_call_id"] = call_id
    return meta


# ── Provider error formatters ─────────────────────────────────────────────────

def _openai_error(decision: str, reason: str) -> Response:
    """Return an error in OpenAI-compatible format."""
    return Response(
        content=json.dumps({
            "error": {
                "message": f"Modus policy: {reason}",
                "type": "modus_policy_error",
                "code": decision,
            }
        }),
        status_code=403 if decision == "deny" else 429,
        media_type="application/json",
    )


def _anthropic_error(decision: str, reason: str) -> Response:
    """Return an error in Anthropic-compatible format."""
    return Response(
        content=json.dumps({
            "type": "error",
            "error": {
                "type": "modus_policy_error",
                "message": f"Modus policy: {reason}",
            }
        }),
        status_code=403 if decision == "deny" else 429,
        media_type="application/json",
    )


# ══════════════════════════════════════════════════════════════════════════════
# OpenAI Proxy
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/openai/v1/chat/completions")
async def openai_chat_completions(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Governed proxy for OpenAI chat completions (streaming + non-streaming)."""
    app = await _resolve_app(request, db)
    body_bytes = await _read_capped_body(request)
    body = json.loads(body_bytes)

    model = body.get("model", "gpt-4o")
    is_stream = body.get("stream", False)
    est_input = _estimate_input_tokens(body)
    est_output = _estimate_output_tokens(body)

    # Policy gate
    decision, reason, suggested_model = await _evaluate_policy(
        app, "openai", model, est_input, est_output, db
    )
    if decision != "allow":
        _gw_count("openai", "chat/completions", decision)
        return _openai_error(decision, reason)

    # Model swap (routing / degradation ladder)
    if suggested_model and suggested_model != model:
        body["model"] = suggested_model
        model = suggested_model

    # Request usage data in streaming responses
    if is_stream:
        body.setdefault("stream_options", {})["include_usage"] = True

    body_bytes = json.dumps(body).encode()
    fwd_headers = _forward_headers(request)
    upstream_url = f"{settings.gateway_openai_base_url}/v1/chat/completions"
    span_meta = _extract_span_meta(request)
    span_meta["endpoint"] = "chat/completions"
    start = time.perf_counter()

    if is_stream:
        budget_limit_usd = await _get_stream_budget_limit(app, db)
        return await _stream_openai(
            app, upstream_url, fwd_headers, body_bytes, model, span_meta, start,
            budget_limit_usd=budget_limit_usd,
        )

    # Non-streaming — circuit breaker + bounded retries on transient failures
    resp = await _upstream_post(
        "openai", "chat/completions", upstream_url, fwd_headers, body_bytes, start
    )

    duration_ms = int((time.perf_counter() - start) * 1000)

    # Extract actual usage from response
    input_tokens, output_tokens = 0, 0
    if resp.status_code == 200:
        try:
            data = resp.json()
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
        except Exception as exc:
            logger.warning(
                "Gateway could not parse OpenAI usage (app_id=%s, model=%s) — "
                "usage recorded with zero tokens: %s",
                getattr(app, "id", "?"), model, exc,
            )

    asyncio.create_task(_record_usage(
        app, "openai", model, input_tokens, output_tokens, duration_ms, span_meta
    ))

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_clean_response_headers(resp.headers),
        media_type=resp.headers.get("content-type", "application/json"),
    )


async def _stream_openai(app, url, headers, body, model, span_meta, start,
                         budget_limit_usd: Decimal = Decimal(0)):
    """Stream OpenAI SSE response with real-time Guillotine enforcement.

    Guillotine Cut: counts tokens in real-time from SSE chunks. The
    nanosecond the cumulative cost hits the budget limit, the stream
    is terminated and a final [DONE] is sent to the client.

    Resilience: the upstream connection is established through the shared
    per-provider circuit breaker. If the circuit is already open we fail fast
    with a 503 BEFORE committing a 200 streaming response; transient connect
    errors during establishment are retried. Once bytes flow to the client no
    retry is possible.
    """
    from orchestrator.core.gateway_resilience import (
        stream_with_resilience, get_breaker,
    )

    # Fail fast on an open circuit while we can still return a real status code.
    if get_breaker().is_open("openai"):
        _gw_count("openai", "chat", "error")
        raise HTTPException(
            503, "Upstream openai temporarily unavailable (circuit open).",
            headers={"Retry-After": str(int(get_breaker().cooldown_remaining("openai")) or 1)},
        )

    async def generate():
        input_tokens = 0
        output_tokens = 0
        guillotine_fired = False
        client = _get_client()

        try:
            async with stream_with_resilience(
                "openai",
                lambda: client.stream("POST", url, headers=headers, content=body),
            ) as resp:
                if resp.status_code != 200:
                    content = await resp.aread()
                    yield content.decode("utf-8", errors="replace")
                    return

                async for line in resp.aiter_lines():
                    # Parse usage from SSE data lines
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            usage = chunk.get("usage")
                            if usage:
                                input_tokens = usage.get("prompt_tokens", input_tokens)
                                output_tokens = usage.get("completion_tokens", output_tokens)

                            # ── Guillotine Cut ──────────────────────────
                            # Only count tokens from deltas when guillotine is armed
                            if budget_limit_usd > 0 and not guillotine_fired:
                                # Real-time token counting from delta chunks
                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content_piece = delta.get("content", "")
                                    if content_piece:
                                        output_tokens += max(len(content_piece) // 4, 1)

                                try:
                                    from orchestrator.core.pricing import estimate_cost
                                    _, _, running_cost = estimate_cost(
                                        "openai", model, input_tokens, output_tokens,
                                    )
                                    if running_cost >= budget_limit_usd:
                                        guillotine_fired = True
                                        _gw_guillotine("openai")
                                        logger.info(
                                            "Guillotine cut: stream terminated (app_id=%s, provider=openai)",
                                            getattr(app, "id", "?"),
                                        )
                                        # Send truncation notice + [DONE]
                                        trunc_chunk = {
                                            "choices": [{
                                                "delta": {"content": "\n\n[Modus: budget limit reached — response truncated]"},
                                                "finish_reason": "modus_budget_limit",
                                                "index": 0,
                                            }]
                                        }
                                        yield f"data: {json.dumps(trunc_chunk)}\n\n"
                                        yield "data: [DONE]\n\n"
                                        # Break inner loop — httpx will close the upstream connection
                                        break
                                except Exception as exc:
                                    # Pricing failure must never silently disable
                                    # mid-stream enforcement. Log with context and
                                    # keep the stream alive (non-blocking law).
                                    logger.warning(
                                        "Guillotine cost estimation failed — mid-stream "
                                        "budget enforcement skipped for this chunk "
                                        "(app_id=%s, provider=openai, model=%s): %s",
                                        getattr(app, "id", "?"), model, exc,
                                    )

                        except (json.JSONDecodeError, KeyError) as exc:
                            logger.debug(
                                "Gateway skipped unparsable OpenAI SSE data line "
                                "(app_id=%s, model=%s): %s",
                                getattr(app, "id", "?"), model, exc,
                            )

                    yield line + "\n"
        except Exception as exc:
            logger.error("Gateway OpenAI stream error: %s", exc)
            yield f"data: {json.dumps({'error': {'message': 'Gateway stream error'}})}\n\n"
            return

        duration_ms = int((time.perf_counter() - start) * 1000)
        asyncio.create_task(_record_usage(
            app, "openai", model, input_tokens, output_tokens, duration_ms,
            {**(span_meta or {}), "guillotine": guillotine_fired},
        ))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/openai/v1/embeddings")
async def openai_embeddings(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Governed proxy for OpenAI embeddings."""
    app = await _resolve_app(request, db)
    body_bytes = await _read_capped_body(request)
    body = json.loads(body_bytes)

    model = body.get("model", "text-embedding-3-small")
    est_input = _estimate_input_tokens(body)

    decision, reason, _ = await _evaluate_policy(
        app, "openai", model, est_input, 0, db
    )
    if decision != "allow":
        _gw_count("openai", "embeddings", decision)
        return _openai_error(decision, reason)

    fwd_headers = _forward_headers(request)
    upstream_url = f"{settings.gateway_openai_base_url}/v1/embeddings"
    start = time.perf_counter()

    resp = await _upstream_post(
        "openai", "embeddings", upstream_url, fwd_headers, body_bytes, start
    )

    duration_ms = int((time.perf_counter() - start) * 1000)

    if resp.status_code == 200:
        try:
            usage = resp.json().get("usage", {})
            asyncio.create_task(_record_usage(
                app, "openai", model, usage.get("prompt_tokens", 0), 0,
                duration_ms, {**_extract_span_meta(request), "endpoint": "embeddings"},
            ))
        except Exception as exc:
            logger.warning(
                "Gateway could not parse OpenAI embeddings usage "
                "(app_id=%s, model=%s) — usage not recorded: %s",
                getattr(app, "id", "?"), model, exc,
            )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_clean_response_headers(resp.headers),
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Anthropic Proxy
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/anthropic/v1/messages")
async def anthropic_messages(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Governed proxy for Anthropic messages (streaming + non-streaming)."""
    app = await _resolve_app(request, db)
    body_bytes = await _read_capped_body(request)
    body = json.loads(body_bytes)

    model = body.get("model", "claude-sonnet-5")
    is_stream = body.get("stream", False)
    est_input = _estimate_input_tokens(body)
    est_output = _estimate_output_tokens(body)

    decision, reason, suggested_model = await _evaluate_policy(
        app, "anthropic", model, est_input, est_output, db
    )
    if decision != "allow":
        _gw_count("anthropic", "messages", decision)
        return _anthropic_error(decision, reason)

    # Model swap
    if suggested_model and suggested_model != model:
        body["model"] = suggested_model
        model = suggested_model

    body_bytes = json.dumps(body).encode()
    fwd_headers = _forward_headers(request)
    upstream_url = f"{settings.gateway_anthropic_base_url}/v1/messages"
    span_meta = _extract_span_meta(request)
    span_meta["endpoint"] = "messages"
    start = time.perf_counter()

    if is_stream:
        budget_limit_usd = await _get_stream_budget_limit(app, db)
        return await _stream_anthropic(
            app, upstream_url, fwd_headers, body_bytes, model, span_meta, start,
            budget_limit_usd=budget_limit_usd,
        )

    resp = await _upstream_post(
        "anthropic", "messages", upstream_url, fwd_headers, body_bytes, start
    )

    duration_ms = int((time.perf_counter() - start) * 1000)

    if resp.status_code == 200:
        try:
            usage = resp.json().get("usage", {})
            asyncio.create_task(_record_usage(
                app, "anthropic", model,
                usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                duration_ms, span_meta,
            ))
        except Exception as exc:
            logger.warning(
                "Gateway could not parse Anthropic usage "
                "(app_id=%s, model=%s) — usage not recorded: %s",
                getattr(app, "id", "?"), model, exc,
            )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_clean_response_headers(resp.headers),
        media_type=resp.headers.get("content-type", "application/json"),
    )


async def _stream_anthropic(app, url, headers, body, model, span_meta, start,
                            budget_limit_usd: Decimal = Decimal(0)):
    """Stream Anthropic SSE response with real-time Guillotine enforcement.

    Guillotine Cut: counts tokens in real-time from SSE events. The
    nanosecond the cumulative cost hits the budget limit, the stream
    is terminated with a proper Anthropic message_stop event.

    Resilience: the upstream connection is established through the shared
    per-provider circuit breaker (fail-fast 503 on open circuit, retried
    connect errors during establishment). See ``_stream_openai``.
    """
    from orchestrator.core.gateway_resilience import (
        stream_with_resilience, get_breaker,
    )

    if get_breaker().is_open("anthropic"):
        _gw_count("anthropic", "messages", "error")
        raise HTTPException(
            503, "Upstream anthropic temporarily unavailable (circuit open).",
            headers={"Retry-After": str(int(get_breaker().cooldown_remaining("anthropic")) or 1)},
        )

    async def generate():
        input_tokens = 0
        output_tokens = 0
        guillotine_fired = False
        client = _get_client()

        try:
            async with stream_with_resilience(
                "anthropic",
                lambda: client.stream("POST", url, headers=headers, content=body),
            ) as resp:
                if resp.status_code != 200:
                    content = await resp.aread()
                    yield content.decode("utf-8", errors="replace")
                    return

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            msg_type = data.get("type")
                            if msg_type == "message_start":
                                usage = data.get("message", {}).get("usage", {})
                                input_tokens = usage.get("input_tokens", 0)
                            elif msg_type == "message_delta":
                                usage = data.get("usage", {})
                                output_tokens = usage.get("output_tokens", output_tokens)
                            # ── Guillotine Cut ──────────────────────────
                            # Only count delta tokens when guillotine is armed
                            if budget_limit_usd > 0 and not guillotine_fired:
                                if msg_type == "content_block_delta":
                                    delta = data.get("delta", {})
                                    text = delta.get("text", "")
                                    if text:
                                        output_tokens += max(len(text) // 4, 1)

                            if budget_limit_usd > 0 and not guillotine_fired:
                                try:
                                    from orchestrator.core.pricing import estimate_cost
                                    _, _, running_cost = estimate_cost(
                                        "anthropic", model, input_tokens, output_tokens,
                                    )
                                    if running_cost >= budget_limit_usd:
                                        guillotine_fired = True
                                        _gw_guillotine("anthropic")
                                        logger.info(
                                            "Guillotine cut: stream terminated (app_id=%s, provider=anthropic)",
                                            getattr(app, "id", "?"),
                                        )
                                        # Send truncation content block + message_stop
                                        trunc_delta = {
                                            "type": "content_block_delta",
                                            "index": 0,
                                            "delta": {"type": "text_delta", "text": "\n\n[Modus: budget limit reached — response truncated]"},
                                        }
                                        yield f"event: content_block_delta\ndata: {json.dumps(trunc_delta)}\n\n"
                                        stop_event = {
                                            "type": "message_delta",
                                            "delta": {"stop_reason": "modus_budget_limit"},
                                            "usage": {"output_tokens": output_tokens},
                                        }
                                        yield f"event: message_delta\ndata: {json.dumps(stop_event)}\n\n"
                                        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                                        break
                                except Exception as exc:
                                    # Pricing failure must never silently disable
                                    # mid-stream enforcement. Log with context and
                                    # keep the stream alive (non-blocking law).
                                    logger.warning(
                                        "Guillotine cost estimation failed — mid-stream "
                                        "budget enforcement skipped for this chunk "
                                        "(app_id=%s, provider=anthropic, model=%s): %s",
                                        getattr(app, "id", "?"), model, exc,
                                    )

                        except (json.JSONDecodeError, KeyError) as exc:
                            logger.debug(
                                "Gateway skipped unparsable Anthropic SSE data line "
                                "(app_id=%s, model=%s): %s",
                                getattr(app, "id", "?"), model, exc,
                            )

                    yield line + "\n"
        except Exception as exc:
            logger.error("Gateway Anthropic stream error: %s", exc)
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'message': 'Gateway stream error'}})}\n\n"
            return

        duration_ms = int((time.perf_counter() - start) * 1000)
        asyncio.create_task(_record_usage(
            app, "anthropic", model, input_tokens, output_tokens, duration_ms,
            {**(span_meta or {}), "guillotine": guillotine_fired},
        ))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Generic Passthrough (non-governed endpoints)
# ══════════════════════════════════════════════════════════════════════════════

@router.api_route(
    "/openai/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def openai_passthrough(
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Passthrough for non-governed OpenAI endpoints (e.g. /v1/models)."""
    await _resolve_app(request, db)  # auth only, no governance
    return await _passthrough(
        request, f"{settings.gateway_openai_base_url}/{path}"
    )


@router.api_route(
    "/anthropic/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def anthropic_passthrough(
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Passthrough for non-governed Anthropic endpoints."""
    await _resolve_app(request, db)
    return await _passthrough(
        request, f"{settings.gateway_anthropic_base_url}/{path}"
    )


async def _passthrough(request: Request, upstream_url: str) -> Response:
    """Forward request to upstream provider without governance."""
    fwd_headers = _forward_headers(request)
    body = await request.body()

    if request.url.query:
        upstream_url += f"?{request.url.query}"

    client = _get_client()
    try:
        resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=fwd_headers,
            content=body if request.method != "GET" else None,
        )
    except httpx.ConnectError:
        raise HTTPException(502, "Failed to connect to upstream provider.")
    except httpx.TimeoutException:
        raise HTTPException(504, "Upstream provider timed out.")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_clean_response_headers(resp.headers),
        media_type=resp.headers.get("content-type", "application/json"),
    )
