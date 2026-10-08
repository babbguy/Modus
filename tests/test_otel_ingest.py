"""
Tests — OTel Trace Ingestion API
Covers: OTLP models, attribute extraction, LLM span detection,
        usage record extraction.
"""
from __future__ import annotations


def test_extract_attr_value_string():
    from orchestrator.api.otel_ingest import _extract_attr_value
    assert _extract_attr_value({"stringValue": "hello"}) == "hello"


def test_extract_attr_value_int():
    from orchestrator.api.otel_ingest import _extract_attr_value
    assert _extract_attr_value({"intValue": "42"}) == 42


def test_extract_attr_value_double():
    from orchestrator.api.otel_ingest import _extract_attr_value
    assert _extract_attr_value({"doubleValue": 3.14}) == 3.14


def test_extract_attr_value_bool():
    from orchestrator.api.otel_ingest import _extract_attr_value
    assert _extract_attr_value({"boolValue": True}) is True


def test_extract_attr_value_unknown():
    from orchestrator.api.otel_ingest import _extract_attr_value
    assert _extract_attr_value({"arrayValue": []}) is None


def test_attrs_to_dict():
    from orchestrator.api.otel_ingest import _attrs_to_dict, OtlpKeyValue
    attrs = [
        OtlpKeyValue(key="gen_ai.system", value={"stringValue": "anthropic"}),
        OtlpKeyValue(key="gen_ai.usage.input_tokens", value={"intValue": "100"}),
    ]
    result = _attrs_to_dict(attrs)
    assert result["gen_ai.system"] == "anthropic"
    assert result["gen_ai.usage.input_tokens"] == 100


def test_is_llm_span_true():
    from orchestrator.api.otel_ingest import _is_llm_span
    assert _is_llm_span({"gen_ai.system": "anthropic"}) is True
    assert _is_llm_span({"llm.provider": "openai"}) is True
    assert _is_llm_span({"gen_ai.request.model": "gpt-4o"}) is True
    assert _is_llm_span({"llm.model": "claude"}) is True


def test_is_llm_span_false():
    from orchestrator.api.otel_ingest import _is_llm_span
    assert _is_llm_span({"http.method": "GET"}) is False
    assert _is_llm_span({}) is False


def test_extract_usage_record_llm_span():
    from orchestrator.api.otel_ingest import _extract_usage_record, OtlpSpan, OtlpKeyValue
    span = OtlpSpan(
        traceId="abc123",
        spanId="span1",
        name="chat",
        startTimeUnixNano="1700000000000000000",
        endTimeUnixNano="1700000001000000000",
        attributes=[
            OtlpKeyValue(key="gen_ai.system", value={"stringValue": "anthropic"}),
            OtlpKeyValue(key="gen_ai.request.model", value={"stringValue": "claude-sonnet-4-5-20250514"}),
            OtlpKeyValue(key="gen_ai.usage.input_tokens", value={"intValue": "500"}),
            OtlpKeyValue(key="gen_ai.usage.output_tokens", value={"intValue": "200"}),
        ],
    )
    record = _extract_usage_record(span)
    assert record is not None
    assert record["provider"] == "anthropic"
    assert record["model"] == "claude-sonnet-4-5-20250514"
    assert record["input_tokens"] == 500
    assert record["output_tokens"] == 200
    assert record["duration_ms"] == 1000
    assert record["metadata"]["source"] == "otlp"


def test_extract_usage_record_langchain_style():
    from orchestrator.api.otel_ingest import _extract_usage_record, OtlpSpan, OtlpKeyValue
    span = OtlpSpan(
        traceId="def456",
        spanId="span2",
        name="llm_call",
        attributes=[
            OtlpKeyValue(key="llm.provider", value={"stringValue": "openai"}),
            OtlpKeyValue(key="llm.model", value={"stringValue": "gpt-4o"}),
            OtlpKeyValue(key="llm.token_count.prompt", value={"intValue": "300"}),
            OtlpKeyValue(key="llm.token_count.completion", value={"intValue": "150"}),
        ],
    )
    record = _extract_usage_record(span)
    assert record is not None
    assert record["provider"] == "openai"
    assert record["input_tokens"] == 300


def test_extract_usage_record_non_llm_span():
    from orchestrator.api.otel_ingest import _extract_usage_record, OtlpSpan, OtlpKeyValue
    span = OtlpSpan(
        traceId="xyz",
        spanId="span3",
        name="http.request",
        attributes=[
            OtlpKeyValue(key="http.method", value={"stringValue": "GET"}),
        ],
    )
    record = _extract_usage_record(span)
    assert record is None


def test_extract_usage_record_no_timestamps():
    from orchestrator.api.otel_ingest import _extract_usage_record, OtlpSpan, OtlpKeyValue
    span = OtlpSpan(
        traceId="t1",
        spanId="s1",
        name="call",
        attributes=[
            OtlpKeyValue(key="gen_ai.system", value={"stringValue": "groq"}),
        ],
    )
    record = _extract_usage_record(span)
    assert record is not None
    assert record["duration_ms"] is None
    assert record["provider"] == "groq"


def test_otlp_trace_request_model():
    from orchestrator.api.otel_ingest import OtlpTraceRequest
    req = OtlpTraceRequest(resourceSpans=[])
    assert req.resourceSpans == []


def test_otlp_span_defaults():
    from orchestrator.api.otel_ingest import OtlpSpan
    span = OtlpSpan()
    assert span.traceId == ""
    assert span.spanId == ""
    assert span.attributes == []


def test_llm_attrs_contains_known_keys():
    from orchestrator.api.otel_ingest import _LLM_ATTRS
    assert "gen_ai.system" in _LLM_ATTRS
    assert "gen_ai.request.model" in _LLM_ATTRS
    assert "llm.provider" in _LLM_ATTRS


# ── OTLP-conformant JSON responses (end-to-end via the handler) ───────────────

def _llm_span_json(model: str = "gpt-4o", provider: str = "openai") -> dict:
    """One OTLP/JSON LLM span the extractor will recognize."""
    return {
        "traceId": "abc", "spanId": "s1", "name": "chat",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000001000000000",
        "attributes": [
            {"key": "gen_ai.system", "value": {"stringValue": provider}},
            {"key": "gen_ai.request.model", "value": {"stringValue": model}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "10"}},
        ],
    }


def _trace_body(spans: list[dict]) -> dict:
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def _install_ingest_stubs(monkeypatch, captured: dict | None = None):
    """Bypass DB app-key verification and the write queue for handler tests."""
    class _App:
        id = "app-1"
        team_id = "team-1"

    async def _fake_verify(raw_key, db):
        return _App()

    monkeypatch.setattr("orchestrator.api.ingest._verify_app_key", _fake_verify)

    async def _fake_enqueue(item):
        if captured is not None:
            captured["records"] = item.records

    monkeypatch.setattr("orchestrator.core.write_queue.enqueue", _fake_enqueue)


async def test_handler_json_full_success_returns_empty_object(client, monkeypatch):
    """A stock OTLP/JSON exporter expects an empty ExportTraceServiceResponse
    (`{}`) on full success — not the legacy `{"accepted": N}` shape."""
    captured: dict = {}
    _install_ingest_stubs(monkeypatch, captured)

    resp = await client.post(
        "/api/v1/v1/traces",
        json=_trace_body([_llm_span_json()]),
        headers={"X-Modus-APIKey": "mds_test_key",
                 "content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {}
    assert "partialSuccess" not in resp.json()
    assert captured["records"][0]["provider"] == "openai"


async def test_handler_json_no_llm_spans_is_full_success(client, monkeypatch):
    """Non-LLM spans are ignored (not rejected) → still full success `{}`."""
    _install_ingest_stubs(monkeypatch)

    non_llm = {"traceId": "t", "spanId": "s", "name": "GET /x",
               "attributes": [{"key": "http.method", "value": {"stringValue": "GET"}}]}
    resp = await client.post(
        "/api/v1/v1/traces",
        json=_trace_body([non_llm]),
        headers={"X-Modus-APIKey": "mds_test_key",
                 "content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {}


async def test_handler_json_over_cap_partial_success(client, monkeypatch):
    """Spans beyond _MAX_SPANS on the JSON path are rejected and surfaced via
    partial_success — not silently dropped, and not left unbounded."""
    _install_ingest_stubs(monkeypatch)
    monkeypatch.setattr("orchestrator.api.otel_ingest._MAX_SPANS", 2)

    # 5 spans, cap 2 → 3 rejected.
    body = _trace_body([_llm_span_json() for _ in range(5)])
    resp = await client.post(
        "/api/v1/v1/traces",
        json=body,
        headers={"X-Modus-APIKey": "mds_test_key",
                 "content-type": "application/json"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["partialSuccess"]["rejectedSpans"] == 3
    assert payload["partialSuccess"]["errorMessage"]


async def test_handler_json_enqueue_failure_rejects_all(client, monkeypatch):
    """If the write queue fails, nothing was persisted → all extracted spans are
    reported rejected so the exporter does not believe they were accepted."""
    class _App:
        id = "app-1"
        team_id = "team-1"

    async def _fake_verify(raw_key, db):
        return _App()

    monkeypatch.setattr("orchestrator.api.ingest._verify_app_key", _fake_verify)

    async def _boom(item):
        raise RuntimeError("queue down")

    monkeypatch.setattr("orchestrator.core.write_queue.enqueue", _boom)

    resp = await client.post(
        "/api/v1/v1/traces",
        json=_trace_body([_llm_span_json(), _llm_span_json()]),
        headers={"X-Modus-APIKey": "mds_test_key",
                 "content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["partialSuccess"]["rejectedSpans"] == 2
