"""
Proof for the stdlib OTLP/protobuf decoder.

Cross-checked against an INDEPENDENT minimal protobuf encoder (separate code
from the decoder — a legitimate wire-format cross-check), plus malformed-input
rejection and an end-to-end handler round-trip.
"""
from __future__ import annotations

import struct

import pytest

from orchestrator.api.otlp_protobuf import (
    decode_traces,
    OtlpDecodeError,
    encode_trace_response,
    decode_trace_response,
)
from orchestrator.api.otel_ingest import OtlpTraceRequest, _extract_usage_record


# ── Independent minimal protobuf encoder (NOT the decoder) ────────────────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _ld(field: int, data: bytes) -> bytes:            # length-delimited
    return _tag(field, 2) + _varint(len(data)) + data


def _str(field: int, s: str) -> bytes:
    return _ld(field, s.encode("utf-8"))


def _fixed64(field: int, n: int) -> bytes:
    return _tag(field, 1) + struct.pack("<Q", n)


def _vint(field: int, n: int) -> bytes:
    return _tag(field, 0) + _varint(n)


def _anyvalue_str(s: str) -> bytes:                   # AnyValue{string_value=1}
    return _str(1, s)


def _anyvalue_int(n: int) -> bytes:                   # AnyValue{int_value=3}
    return _vint(3, n)


def _keyvalue(key: str, value_msg: bytes) -> bytes:   # KeyValue{key=1,value=2}
    return _str(1, key) + _ld(2, value_msg)


def _span(name: str, start: int, end: int, attrs: list[bytes]) -> bytes:
    body = _str(5, name) + _fixed64(7, start) + _fixed64(8, end)
    for a in attrs:
        body += _ld(9, a)                              # attributes = 9
    return body


def _export_request(span_bytes: bytes) -> bytes:
    scope_spans = _ld(2, span_bytes)                   # ScopeSpans.spans = 2
    resource_spans = _ld(2, scope_spans)               # ResourceSpans.scope_spans = 2
    return _ld(1, resource_spans)                       # request.resource_spans = 1


# ── 1. Round-trip: a real GenAI span decodes to the right usage record ────────

def test_protobuf_genai_span_roundtrip():
    attrs = [
        _keyvalue("gen_ai.system", _anyvalue_str("openai")),
        _keyvalue("gen_ai.request.model", _anyvalue_str("gpt-4o")),
        _keyvalue("gen_ai.usage.input_tokens", _anyvalue_int(1200)),
        _keyvalue("gen_ai.usage.output_tokens", _anyvalue_int(340)),
    ]
    start_ns = 1_700_000_000_000_000_000
    end_ns = start_ns + 250_000_000  # +250ms
    payload = _export_request(_span("chat gpt-4o", start_ns, end_ns, attrs))

    decoded = decode_traces(payload)
    req = OtlpTraceRequest(**decoded)
    spans = req.resourceSpans[0].scopeSpans[0].spans
    assert len(spans) == 1

    rec = _extract_usage_record(spans[0])
    assert rec is not None
    assert rec["provider"] == "openai"
    assert rec["model"] == "gpt-4o"
    assert rec["input_tokens"] == 1200
    assert rec["output_tokens"] == 340
    assert rec["duration_ms"] == 250
    assert rec["metadata"]["source"] == "otlp"


def test_protobuf_reported_cost_extracted():
    # AnyValue double_value (field 4, wire 1)
    cost_val = struct.pack("<d", 0.0123)
    anyvalue_double = _tag(4, 1) + cost_val
    attrs = [
        _keyvalue("gen_ai.system", _anyvalue_str("anthropic")),
        _keyvalue("gen_ai.request.model", _anyvalue_str("claude-sonnet-4-5")),
        _keyvalue("gen_ai.usage.cost", anyvalue_double),
    ]
    payload = _export_request(_span("chat", 1, 2, attrs))
    rec = _extract_usage_record(
        OtlpTraceRequest(**decode_traces(payload)).resourceSpans[0].scopeSpans[0].spans[0]
    )
    assert rec["reported_cost"] == pytest.approx(0.0123)


def test_protobuf_non_llm_span_ignored():
    attrs = [_keyvalue("http.method", _anyvalue_str("GET"))]
    payload = _export_request(_span("GET /x", 1, 2, attrs))
    spans = OtlpTraceRequest(**decode_traces(payload)).resourceSpans[0].scopeSpans[0].spans
    assert _extract_usage_record(spans[0]) is None


# ── 2. Malformed / hostile input is rejected, never hangs ─────────────────────

def test_truncated_varint_rejected():
    with pytest.raises(OtlpDecodeError):
        decode_traces(b"\x08\x80\x80")  # varint with continuation bit, no end


def test_truncated_length_delimited_rejected():
    # field 1, wire 2, length=100, but no payload
    with pytest.raises(OtlpDecodeError):
        decode_traces(_tag(1, 2) + _varint(100) + b"short")


def test_empty_input_is_empty_result():
    decoded = decode_traces(b"")
    assert decoded["resourceSpans"][0]["scopeSpans"][0]["spans"] == []


def test_unknown_fields_skipped():
    # A span with an unknown extra field (field 99, varint) must still decode.
    span = _span("chat", 1, 2, [_keyvalue("gen_ai.system", _anyvalue_str("openai"))])
    span += _vint(99, 12345)  # unknown field — must be skipped, not crash
    payload = _export_request(span)
    spans = OtlpTraceRequest(**decode_traces(payload)).resourceSpans[0].scopeSpans[0].spans
    assert _extract_usage_record(spans[0])["provider"] == "openai"


# ── 3. End-to-end: stock protobuf exporter content-type is accepted ───────────

async def test_handler_accepts_protobuf(client, monkeypatch):
    # Bypass DB app-key verification; focus on the protobuf ingest path.
    class _App:
        id = "app-1"
        team_id = "team-1"

    async def _fake_verify(raw_key, db):
        return _App()

    monkeypatch.setattr("orchestrator.api.ingest._verify_app_key", _fake_verify)

    captured = {}

    async def _fake_enqueue(item):
        captured["records"] = item.records

    monkeypatch.setattr("orchestrator.core.write_queue.enqueue", _fake_enqueue)

    attrs = [
        _keyvalue("gen_ai.system", _anyvalue_str("openai")),
        _keyvalue("gen_ai.request.model", _anyvalue_str("gpt-4o")),
        _keyvalue("gen_ai.usage.input_tokens", _anyvalue_int(50)),
    ]
    payload = _export_request(_span("chat", 1, 2, attrs))

    resp = await client.post(
        "/api/v1/v1/traces", content=payload,
        headers={"X-Modus-APIKey": "mds_test_key",
                 "content-type": "application/x-protobuf"},
    )
    assert resp.status_code == 200
    # A stock OTLP exporter reads an ExportTraceServiceResponse back. Full
    # success is an EMPTY protobuf message with the protobuf content-type.
    assert resp.headers["content-type"].startswith("application/x-protobuf")
    assert resp.content == b""
    rejected, message = decode_trace_response(resp.content)
    assert rejected == 0
    assert message == ""
    assert captured["records"][0]["provider"] == "openai"


# ── 4. ExportTraceServiceResponse encoder round-trips ─────────────────────────

def test_encode_trace_response_full_success_is_empty():
    # Full success (nothing rejected) → empty message, per OTLP.
    assert encode_trace_response(0, "") == b""
    assert encode_trace_response(0, "ignored") == b""
    assert decode_trace_response(b"") == (0, "")


def test_encode_trace_response_partial_success_roundtrip():
    body = encode_trace_response(7, "7 span(s) rejected")
    assert body != b""  # partial success carries a payload
    rejected, message = decode_trace_response(body)
    assert rejected == 7
    assert message == "7 span(s) rejected"


def test_encode_trace_response_partial_no_message_roundtrip():
    body = encode_trace_response(3)
    rejected, message = decode_trace_response(body)
    assert rejected == 3
    assert message == ""


def test_encode_trace_response_large_count_roundtrip():
    body = encode_trace_response(1_234_567, "capped")
    assert decode_trace_response(body) == (1_234_567, "capped")


# ── 5. End-to-end: over-cap protobuf export gets a protobuf partial_success ────

async def test_handler_protobuf_over_cap_partial_success(client, monkeypatch):
    class _App:
        id = "app-1"
        team_id = "team-1"

    async def _fake_verify(raw_key, db):
        return _App()

    monkeypatch.setattr("orchestrator.api.ingest._verify_app_key", _fake_verify)

    async def _fake_enqueue(item):
        return None

    monkeypatch.setattr("orchestrator.core.write_queue.enqueue", _fake_enqueue)
    # Shrink the shared cap so the test stays small.
    monkeypatch.setattr("orchestrator.api.otel_ingest._MAX_SPANS", 2)

    attrs = [_keyvalue("gen_ai.system", _anyvalue_str("openai"))]
    # Three spans in one ScopeSpans → one over the cap of 2.
    scope_spans_body = b"".join(_ld(2, _span("chat", 1, 2, attrs)) for _ in range(3))
    resource_spans_body = _ld(2, scope_spans_body)   # ResourceSpans.scope_spans = 2
    payload = _ld(1, resource_spans_body)            # request.resource_spans = 1

    resp = await client.post(
        "/api/v1/v1/traces", content=payload,
        headers={"X-Modus-APIKey": "mds_test_key",
                 "content-type": "application/x-protobuf"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-protobuf")
    rejected, message = decode_trace_response(resp.content)
    assert rejected == 1
    assert message  # a human-readable reason is included
