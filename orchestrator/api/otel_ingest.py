"""
OTel Trace Ingestion — accepts OTLP/HTTP traces and extracts cost data.

Teams using Langfuse, LangSmith, or any OTel exporter can send a copy
of their traces to Modus for cost attribution.

Endpoint: POST /v1/traces (OTLP/HTTP JSON format)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from orchestrator.core.auth import get_app_identity

# Single shared span cap across both ingest paths (JSON + protobuf). The
# protobuf decoder enforces it while decoding; the JSON path enforces it in the
# triple loop below. Importing the one constant keeps them from drifting.
from orchestrator.api.otlp_protobuf import _MAX_SPANS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["otel"])


# ── OTLP JSON models (simplified) ────────────────────────────────────────────

class OtlpKeyValue(BaseModel):
    key: str
    value: dict[str, Any]


class OtlpSpan(BaseModel):
    traceId: str = ""
    spanId: str = ""
    parentSpanId: str = ""
    name: str = ""
    startTimeUnixNano: Optional[str] = None
    endTimeUnixNano: Optional[str] = None
    attributes: list[OtlpKeyValue] = []


class OtlpScopeSpans(BaseModel):
    spans: list[OtlpSpan] = []


class OtlpResourceSpans(BaseModel):
    scopeSpans: list[OtlpScopeSpans] = []


class OtlpTraceRequest(BaseModel):
    resourceSpans: list[OtlpResourceSpans] = []


# ── Attribute extraction ──────────────────────────────────────────────────────

# Known LLM span attribute keys (OpenTelemetry Semantic Conventions for GenAI)
_LLM_ATTRS = {
    "gen_ai.system",
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cost",
    "gen_ai.operation.name",
    # LangChain/Langfuse style
    "llm.provider",
    "llm.model",
    "llm.token_count.prompt",
    "llm.token_count.completion",
}


def _extract_attr_value(value: dict) -> Any:
    """Extract a typed value from an OTLP attribute value dict."""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return value["boolValue"]
    return None


def _attrs_to_dict(attributes: list[OtlpKeyValue]) -> dict[str, Any]:
    """Convert OTLP attributes list to a flat dict."""
    return {kv.key: _extract_attr_value(kv.value) for kv in attributes}


def _is_llm_span(attrs: dict[str, Any]) -> bool:
    """Check if a span represents an LLM call based on its attributes."""
    return bool(
        attrs.get("gen_ai.system")
        or attrs.get("llm.provider")
        or attrs.get("gen_ai.request.model")
        or attrs.get("llm.model")
    )


def _extract_usage_record(span: OtlpSpan) -> Optional[dict]:
    """Extract a Modus usage record from an OTLP LLM span."""
    attrs = _attrs_to_dict(span.attributes)

    if not _is_llm_span(attrs):
        return None

    provider = attrs.get("gen_ai.system") or attrs.get("llm.provider") or "unknown"
    model = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model") or attrs.get("llm.model")
    input_tokens = (
        attrs.get("gen_ai.usage.input_tokens")
        or attrs.get("gen_ai.usage.prompt_tokens")
        or attrs.get("llm.token_count.prompt")
    )
    output_tokens = (
        attrs.get("gen_ai.usage.output_tokens")
        or attrs.get("gen_ai.usage.completion_tokens")
        or attrs.get("llm.token_count.completion")
    )
    # Authoritative cost, when the exporter provides it (was listed but dropped).
    reported_cost = attrs.get("gen_ai.usage.cost")
    operation = attrs.get("gen_ai.operation.name") or span.name

    # Calculate duration from span timestamps
    duration_ms = None
    if span.startTimeUnixNano and span.endTimeUnixNano:
        try:
            start_ns = int(span.startTimeUnixNano)
            end_ns = int(span.endTimeUnixNano)
            duration_ms = int((end_ns - start_ns) / 1_000_000)
        except (ValueError, TypeError):
            pass

    # Parse timestamp
    timestamp = datetime.now(timezone.utc).isoformat()
    if span.startTimeUnixNano:
        try:
            start_ns = int(span.startTimeUnixNano)
            timestamp = datetime.fromtimestamp(
                start_ns / 1_000_000_000, tz=timezone.utc
            ).isoformat()
        except (ValueError, TypeError, OSError):
            pass

    return {
        "provider": provider,
        "model": model,
        "operation": operation,
        "resource_type": "llm_call",
        "input_tokens": int(input_tokens) if input_tokens is not None else None,
        "output_tokens": int(output_tokens) if output_tokens is not None else None,
        "reported_cost": float(reported_cost) if reported_cost is not None else None,
        "duration_ms": duration_ms,
        "timestamp": timestamp,
        "metadata": {
            "source": "otlp",
            "trace_id": span.traceId,
            "span_id": span.spanId,
        },
    }


# ── OTLP-conformant response helpers ──────────────────────────────────────────

def _export_response(
    *,
    is_protobuf: bool,
    rejected_spans: int,
    error_message: str,
):
    """Build an OTLP-conformant ExportTraceServiceResponse.

    Full success (``rejected_spans == 0``) → an empty message: ``{}`` for JSON,
    empty bytes for protobuf. Partial success carries the rejected-span count and
    a reason. Always HTTP 200 — a partial success is still a successful export.
    """
    if is_protobuf:
        from orchestrator.api.otlp_protobuf import encode_trace_response
        return Response(
            content=encode_trace_response(rejected_spans, error_message),
            media_type="application/x-protobuf",
            status_code=200,
        )
    if rejected_spans > 0:
        return {
            "partialSuccess": {
                "rejectedSpans": rejected_spans,
                "errorMessage": error_message,
            }
        }
    return {}


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/v1/traces", status_code=200)
async def ingest_traces(
    request: Request,
    raw_key: str = Depends(get_app_identity),
):
    """
    Accept OTLP/HTTP traces (JSON **or** protobuf) and extract LLM cost data.

    Content negotiation by header:
      - `application/x-protobuf` (the stock OTel exporter default) → decoded by
        the stdlib OTLP protobuf reader, and the response is an
        `application/x-protobuf` ExportTraceServiceResponse.
      - otherwise → OTLP/JSON, with a JSON ExportTraceServiceResponse.

    The response is OTLP-conformant so a stock OpenTelemetry exporter works with
    no surprises: an empty message on full success, or a `partial_success`
    carrying the rejected-span count when spans were capped or could not be
    persisted. Both paths converge on the same span-extraction logic and share a
    single `_MAX_SPANS` cap. Non-LLM spans are silently ignored (not rejected —
    they are simply not cost-relevant). Requires app-level authentication (same
    as /api/v1/ingest).
    """
    # Resolve app identity from API key (same pattern as ingest endpoint)
    from orchestrator.db.session import get_session_ctx
    from orchestrator.api.ingest import _verify_app_key

    async with get_session_ctx() as db:
        app = await _verify_app_key(raw_key, db)

    content_type = request.headers.get("content-type", "").lower()
    is_protobuf = (
        "application/x-protobuf" in content_type
        or "application/protobuf" in content_type
    )
    raw_body = await request.body()

    if is_protobuf:
        from orchestrator.api.otlp_protobuf import decode_traces, OtlpDecodeError
        try:
            body = decode_traces(raw_body)
        except OtlpDecodeError as exc:
            logger.debug("OTel ingest: invalid protobuf: %s", exc)
            return {"accepted": 0, "error": "Invalid OTLP protobuf"}
    else:
        import json as _json
        try:
            body = _json.loads(raw_body)
        except Exception:
            return {"accepted": 0, "error": "Invalid JSON"}

    try:
        trace_req = OtlpTraceRequest(**body)
    except Exception:
        return {"accepted": 0, "error": "Invalid OTLP format"}

    # Extract usage records, capping the JSON path at the same _MAX_SPANS the
    # protobuf decoder enforces. Spans beyond the cap are counted as rejected and
    # surfaced through partial_success rather than silently dropped.
    records: list[dict] = []
    spans_processed = 0
    rejected_spans = 0
    for rs in trace_req.resourceSpans:
        for ss in rs.scopeSpans:
            for span in ss.spans:
                if spans_processed >= _MAX_SPANS:
                    rejected_spans += 1
                    continue
                spans_processed += 1
                rec = _extract_usage_record(span)
                if rec:
                    records.append(rec)

    # Enqueue extracted records through the standard write queue.
    batch_id: Optional[str] = None
    if records:
        try:
            from orchestrator.core.write_queue import enqueue, IngestItem
            batch_id = str(uuid4())
            await enqueue(IngestItem(
                app_id=str(app.id),
                team_id=str(app.team_id),
                batch_id=batch_id,
                records=records,
                record_count=len(records),
                agent_version="otlp-bridge",
                sdk_versions={},
            ))
        except Exception as exc:
            logger.error("OTel ingest: write queue error: %s", exc)
            # None of the extracted LLM spans were persisted → reject them all so
            # the exporter learns nothing was accepted.
            rejected_spans += len(records)
            records = []
            batch_id = None

    accepted = len(records)
    error_message = ""
    if rejected_spans > 0:
        error_message = (
            f"{rejected_spans} span(s) rejected: exceeded the per-request cap of "
            f"{_MAX_SPANS} or could not be persisted."
        )
        logger.warning(
            "OTel ingest: %d LLM spans accepted, %d rejected (cap=%d, batch %s)",
            accepted, rejected_spans, _MAX_SPANS, batch_id,
        )
    else:
        logger.info(
            "OTel ingest: %d LLM spans accepted (batch %s)", accepted, batch_id
        )

    return _export_response(
        is_protobuf=is_protobuf,
        rejected_spans=rejected_spans,
        error_message=error_message,
    )
