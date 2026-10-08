"""
Modus — Minimal OTLP/Protobuf Traces Decoder (stdlib only)
=============================================================
Decodes the subset of the OTLP `ExportTraceServiceRequest` protobuf message
that Modus consumes, using **only the Python standard library** — no
`protobuf`, no `opentelemetry-proto`, no generated code, no new dependency.

Why hand-rolled instead of a dependency: the orchestrator's edge is a small,
supply-chain-clean footprint. The OTLP Traces schema is stable and versioned,
and we only read a handful of fields (span name, start/end nanos, and
attributes), so a focused ~150-line reader is a better trade than pulling in a
protobuf toolchain. It emits the exact same dict shape the JSON path produces,
so the ingest handler stays a single code path.

Robustness: this parses untrusted network input. Every read is bounds-checked,
position always advances (no infinite loops), recursion is depth-limited, and
span/attribute counts are capped so a hostile payload cannot exhaust memory.

Protobuf wire format reference (only what we need):
  tag        = varint; field_number = tag >> 3; wire_type = tag & 7
  wire 0     = varint          (int / bool / enum)
  wire 1     = 64-bit          (fixed64 / double)
  wire 2     = length-delimited (string / bytes / embedded message)
  wire 5     = 32-bit          (fixed32 / float)   -- skipped, unused here
"""
from __future__ import annotations

import struct
from typing import Any, Iterator, Optional

# Caps: a hostile payload must not exhaust memory. Generous for real batches.
_MAX_SPANS = 10_000
_MAX_ATTRS_PER_SPAN = 512
_MAX_DEPTH = 8


class OtlpDecodeError(ValueError):
    """Raised on malformed OTLP protobuf input."""


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint. Returns (value, new_pos). Bounds-checked."""
    result = 0
    shift = 0
    n = len(data)
    while True:
        if pos >= n:
            raise OtlpDecodeError("truncated varint")
        if shift > 63:
            raise OtlpDecodeError("varint too long")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _iter_fields(data: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield (field_number, wire_type, value) for each field in a message.

    value is: an int for wire 0, an 8-byte slice for wire 1, a bytes slice for
    wire 2, a 4-byte slice for wire 5. Groups (wire 3/4) are rejected.
    """
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _read_varint(data, pos)
        field_num = tag >> 3
        wire = tag & 0x07
        if wire == 0:  # varint
            val, pos = _read_varint(data, pos)
            yield field_num, wire, val
        elif wire == 1:  # 64-bit
            if pos + 8 > n:
                raise OtlpDecodeError("truncated 64-bit field")
            yield field_num, wire, data[pos:pos + 8]
            pos += 8
        elif wire == 2:  # length-delimited
            length, pos = _read_varint(data, pos)
            if length < 0 or pos + length > n:
                raise OtlpDecodeError("truncated length-delimited field")
            yield field_num, wire, data[pos:pos + length]
            pos += length
        elif wire == 5:  # 32-bit
            if pos + 4 > n:
                raise OtlpDecodeError("truncated 32-bit field")
            yield field_num, wire, data[pos:pos + 4]
            pos += 4
        else:
            raise OtlpDecodeError(f"unsupported wire type {wire}")


def _decode_anyvalue(data: bytes) -> dict:
    """Decode an AnyValue message into the JSON-path value dict shape."""
    for field_num, wire, val in _iter_fields(data):
        if field_num == 1 and wire == 2:      # string_value
            return {"stringValue": val.decode("utf-8", "replace")}
        if field_num == 2 and wire == 0:      # bool_value
            return {"boolValue": bool(val)}
        if field_num == 3 and wire == 0:      # int_value (varint, zigzag? no — int64)
            # OTLP int_value is a plain int64; varint encodes it two's-complement.
            return {"intValue": val - (1 << 64) if val >= (1 << 63) else val}
        if field_num == 4 and wire == 1:      # double_value
            return {"doubleValue": struct.unpack("<d", val)[0]}
        # array_value (5) / kvlist_value (6) / bytes_value (7): not consumed.
    return {}


def _decode_keyvalue(data: bytes, depth: int) -> Optional[dict]:
    """Decode a KeyValue message -> {"key": str, "value": {...}}."""
    key = ""
    value: dict = {}
    for field_num, wire, val in _iter_fields(data):
        if field_num == 1 and wire == 2:
            key = val.decode("utf-8", "replace")
        elif field_num == 2 and wire == 2 and depth < _MAX_DEPTH:
            value = _decode_anyvalue(val)
    if not key:
        return None
    return {"key": key, "value": value}


def _decode_span(data: bytes, depth: int) -> dict:
    """Decode a Span message -> the JSON-path span dict shape."""
    span: dict = {
        "traceId": "", "spanId": "", "parentSpanId": "", "name": "",
        "startTimeUnixNano": None, "endTimeUnixNano": None, "attributes": [],
    }
    for field_num, wire, val in _iter_fields(data):
        if field_num == 1 and wire == 2:       # trace_id (bytes)
            span["traceId"] = val.hex()
        elif field_num == 2 and wire == 2:     # span_id (bytes)
            span["spanId"] = val.hex()
        elif field_num == 4 and wire == 2:     # parent_span_id (bytes)
            span["parentSpanId"] = val.hex()
        elif field_num == 5 and wire == 2:     # name (string)
            span["name"] = val.decode("utf-8", "replace")
        elif field_num == 7 and wire == 1:     # start_time_unix_nano (fixed64)
            span["startTimeUnixNano"] = str(struct.unpack("<Q", val)[0])
        elif field_num == 8 and wire == 1:     # end_time_unix_nano (fixed64)
            span["endTimeUnixNano"] = str(struct.unpack("<Q", val)[0])
        elif field_num == 9 and wire == 2:     # attributes (repeated KeyValue)
            if len(span["attributes"]) < _MAX_ATTRS_PER_SPAN and depth < _MAX_DEPTH:
                kv = _decode_keyvalue(val, depth + 1)
                if kv is not None:
                    span["attributes"].append(kv)
    return span


def decode_traces(data: bytes) -> dict:
    """Decode an OTLP/protobuf ExportTraceServiceRequest into the same dict
    shape the JSON path produces: {"resourceSpans": [{"scopeSpans": [{"spans":
    [...]}]}]}. Raises OtlpDecodeError on malformed input.
    """
    spans: list[dict] = []

    # ExportTraceServiceRequest.resource_spans = 1 (repeated ResourceSpans)
    for f1, w1, rs in _iter_fields(data):
        if f1 != 1 or w1 != 2:
            continue
        # ResourceSpans.scope_spans = 2 (repeated ScopeSpans)
        for f2, w2, ss in _iter_fields(rs):
            if f2 != 2 or w2 != 2:
                continue
            # ScopeSpans.spans = 2 (repeated Span)
            for f3, w3, sp in _iter_fields(ss):
                if f3 != 2 or w3 != 2:
                    continue
                if len(spans) >= _MAX_SPANS:
                    raise OtlpDecodeError(f"too many spans (> {_MAX_SPANS})")
                spans.append(_decode_span(sp, depth=0))

    # Re-nest into the shape OtlpTraceRequest expects (grouping is irrelevant to
    # extraction, so a single resource/scope wrapper is fine).
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


# ── ExportTraceServiceResponse encoder / decoder ──────────────────────────────
#
# A stock OTLP exporter reads the response body back as an
# ExportTraceServiceResponse. Full success is an EMPTY message; partial success
# carries a nested ExportTracePartialSuccess with the rejected-span count and a
# human-readable reason. We hand-roll the writer with the same wire-format style
# as the reader above — no protobuf dependency.
#
#   message ExportTraceServiceResponse {
#     ExportTracePartialSuccess partial_success = 1;   // absent on full success
#   }
#   message ExportTracePartialSuccess {
#     int64  rejected_spans = 1;
#     string error_message  = 2;
#   }


def _write_varint(value: int) -> bytes:
    """Encode a non-negative int as a base-128 varint (int64 two's-complement)."""
    if value < 0:
        value += 1 << 64  # int64 two's-complement, mirrors the reader
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _write_tag(field_num: int, wire: int) -> bytes:
    return _write_varint((field_num << 3) | wire)


def encode_trace_response(rejected_spans: int, error_message: str = "") -> bytes:
    """Encode an ExportTraceServiceResponse.

    Full success (``rejected_spans == 0``) → an empty message (``b""``), exactly
    what an OTLP exporter expects. Otherwise → a ``partial_success`` message
    carrying the rejected-span count and an optional reason string.
    """
    if rejected_spans <= 0:
        return b""

    # ExportTracePartialSuccess body
    partial = bytearray()
    partial += _write_tag(1, 0) + _write_varint(rejected_spans)   # rejected_spans
    if error_message:
        msg = error_message.encode("utf-8")
        partial += _write_tag(2, 2) + _write_varint(len(msg)) + msg  # error_message

    partial_bytes = bytes(partial)
    # ExportTraceServiceResponse.partial_success = 1 (embedded message)
    return _write_tag(1, 2) + _write_varint(len(partial_bytes)) + partial_bytes


def decode_trace_response(data: bytes) -> tuple[int, str]:
    """Decode an ExportTraceServiceResponse → ``(rejected_spans, error_message)``.

    Empty input (full success) → ``(0, "")``. Symmetric with
    :func:`encode_trace_response`; used by exporters and by the test suite to
    read the response back. Raises :class:`OtlpDecodeError` on malformed input.
    """
    rejected = 0
    message = ""
    for f1, w1, val in _iter_fields(data):
        if f1 == 1 and w1 == 2:  # partial_success (embedded message)
            for f2, w2, v2 in _iter_fields(val):
                if f2 == 1 and w2 == 0:       # rejected_spans (varint)
                    rejected = v2
                elif f2 == 2 and w2 == 2:     # error_message (string)
                    message = v2.decode("utf-8", "replace")
    return rejected, message
