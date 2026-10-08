"""
Tests for orchestrator.core.attribution_engine — Shapley attribution, retry/defensive detection.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orchestrator.core.attribution_engine import (
    _build_graph,
    _build_graph_json,
    _compute_attribution,
    _detect_defensive,
    _detect_retries,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _span(
    call_id: str,
    parent_call_id: str | None = None,
    provider: str = "openai",
    model: str = "gpt-4o",
    total_cost: str = "0.10",
    **kw,
) -> dict:
    base = {
        "call_id": call_id,
        "parent_call_id": parent_call_id,
        "session_id": "sess-1",
        "provider": provider,
        "model": model,
        "input_tokens": kw.get("input_tokens", 100),
        "output_tokens": kw.get("output_tokens", 50),
        "total_cost": Decimal(total_cost),
        "duration_ms": kw.get("duration_ms", 500),
        "timestamp": kw.get("timestamp", datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)),
        "app_id": "app-1",
        "team_id": "team-1",
        "span_name": kw.get("span_name"),
        "fw_tier": kw.get("fw_tier", "custom"),
    }
    base.update(kw)
    return base


# ── _build_graph ─────────────────────────────────────────────────────────────


class TestBuildGraph:
    def test_single_node(self):
        spans = [_span("a")]
        graph = _build_graph(spans)
        assert "a" in graph
        assert graph["a"]["parent"] is None
        assert graph["a"]["children"] == []

    def test_parent_child(self):
        spans = [_span("a"), _span("b", parent_call_id="a")]
        graph = _build_graph(spans)
        assert "b" in graph["a"]["children"]
        assert graph["b"]["parent"] == "a"

    def test_multi_children(self):
        spans = [
            _span("root"),
            _span("c1", parent_call_id="root"),
            _span("c2", parent_call_id="root"),
        ]
        graph = _build_graph(spans)
        assert len(graph["root"]["children"]) == 2


# ── _detect_retries ──────────────────────────────────────────────────────────


class TestDetectRetries:
    def test_consecutive_same_model_detected(self):
        t1 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 9, 12, 0, 1, tzinfo=timezone.utc)
        spans = [
            _span("root"),
            _span("c1", parent_call_id="root", timestamp=t1),
            _span("c2", parent_call_id="root", timestamp=t2),  # same model = retry
        ]
        graph = _build_graph(spans)
        _detect_retries(graph, spans)

        assert spans[1].get("is_retry") is not True  # first call is not retry
        assert spans[2].get("is_retry") is True

    def test_different_models_not_retries(self):
        t1 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 9, 12, 0, 1, tzinfo=timezone.utc)
        spans = [
            _span("root"),
            _span("c1", parent_call_id="root", model="gpt-4o", timestamp=t1),
            _span("c2", parent_call_id="root", model="gpt-4o-mini", timestamp=t2),
        ]
        graph = _build_graph(spans)
        _detect_retries(graph, spans)

        assert spans[1].get("is_retry") is not True
        assert spans[2].get("is_retry") is not True


# ── _detect_defensive ────────────────────────────────────────────────────────


class TestDetectDefensive:
    def test_fallback_after_retry(self):
        t1 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 9, 12, 0, 1, tzinfo=timezone.utc)
        t3 = datetime(2026, 4, 9, 12, 0, 2, tzinfo=timezone.utc)
        spans = [
            _span("root"),
            _span("c1", parent_call_id="root", model="gpt-4o", timestamp=t1),
            _span("c2", parent_call_id="root", model="gpt-4o", timestamp=t2),  # retry
            _span("c3", parent_call_id="root", model="gpt-4o-mini", timestamp=t3),  # fallback
        ]
        graph = _build_graph(spans)
        _detect_retries(graph, spans)
        _detect_defensive(graph, spans)

        assert spans[2].get("is_retry") is True
        assert spans[3].get("is_defensive") is True

    def test_no_defensive_without_retry(self):
        spans = [
            _span("root"),
            _span("c1", parent_call_id="root", model="gpt-4o"),
            _span("c2", parent_call_id="root", model="gpt-4o-mini"),
        ]
        graph = _build_graph(spans)
        _detect_retries(graph, spans)
        _detect_defensive(graph, spans)

        assert spans[1].get("is_defensive") is not True
        assert spans[2].get("is_defensive") is not True


# ── _compute_attribution ─────────────────────────────────────────────────────


class TestComputeAttribution:
    def test_single_node_attribution(self):
        spans = [_span("a", total_cost="1.00")]
        graph = _build_graph(spans)
        _compute_attribution(graph, spans)

        assert spans[0]["attributed_cost"] == Decimal("1.00")
        assert spans[0]["amplification_factor"] == 0.0

    def test_parent_accumulates_children(self):
        spans = [
            _span("root", total_cost="1.00"),
            _span("c1", parent_call_id="root", total_cost="0.50"),
            _span("c2", parent_call_id="root", total_cost="0.30"),
        ]
        graph = _build_graph(spans)
        _compute_attribution(graph, spans)

        root = spans[0]
        assert root["attributed_cost"] == Decimal("1.80")
        assert root["amplification_factor"] == pytest.approx(0.8, abs=0.01)

    def test_retry_tax_attributed_to_parent(self):
        t1 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 9, 12, 0, 1, tzinfo=timezone.utc)
        spans = [
            _span("root", total_cost="1.00"),
            _span("c1", parent_call_id="root", total_cost="0.50", timestamp=t1),
            _span("c2", parent_call_id="root", total_cost="0.50", timestamp=t2),  # retry
        ]
        graph = _build_graph(spans)
        _detect_retries(graph, spans)
        _compute_attribution(graph, spans)

        assert spans[0].get("retry_tax", Decimal("0")) == Decimal("0.50")

    def test_deep_tree(self):
        spans = [
            _span("a", total_cost="1.00"),
            _span("b", parent_call_id="a", total_cost="0.50"),
            _span("c", parent_call_id="b", total_cost="0.25"),
        ]
        graph = _build_graph(spans)
        _compute_attribution(graph, spans)

        # c = 0.25, b = 0.50 + 0.25 = 0.75, a = 1.00 + 0.75 = 1.75
        assert spans[2]["attributed_cost"] == Decimal("0.25")
        assert spans[1]["attributed_cost"] == Decimal("0.75")
        assert spans[0]["attributed_cost"] == Decimal("1.75")


# ── _build_graph_json ────────────────────────────────────────────────────────


class TestBuildGraphJson:
    def test_structure(self):
        spans = [
            _span("a"),
            _span("b", parent_call_id="a"),
        ]
        graph = _build_graph(spans)
        _compute_attribution(graph, spans)
        result = _build_graph_json(spans, graph)

        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1
        assert result["edges"][0]["from"] == "a"
        assert result["edges"][0]["to"] == "b"


# ── Database path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_session_reads_span_ids_from_record_metadata(db_session, registered_app):
    """The engine must read each record's JSON metadata (``metadata_``), not the
    declarative base's ``metadata`` attribute, and persist the attribution."""
    import uuid as _uuid
    from datetime import timedelta

    from sqlalchemy import select

    from orchestrator.core.attribution_engine import _process_session
    from orchestrator.db.models import AttributionNode, AttributionSession, UsageRecord

    session_id = str(_uuid.uuid4())
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    for i, (call_id, parent) in enumerate([("root", None), ("child", "root")]):
        db_session.add(UsageRecord(
            app_id=registered_app["app_uuid"], team_id=registered_app["team_id"],
            provider="anthropic", resource_type="llm_call", model="claude-sonnet-5-5",
            input_tokens=100, output_tokens=50, total_tokens=150,
            total_cost=Decimal("0.01"), timestamp=start + timedelta(seconds=i),
            session_id=session_id,
            metadata_={"mds_call_id": call_id, "mds_parent_id": parent},
        ))
    await db_session.commit()

    await _process_session(db_session, session_id, start, start + timedelta(seconds=1))
    await db_session.commit()

    saved = (await db_session.execute(
        select(AttributionSession).where(AttributionSession.session_id == session_id)
    )).scalar_one()
    assert saved is not None
    nodes = (await db_session.execute(
        select(AttributionNode).where(AttributionNode.session_id == session_id)
    )).scalars().all()
    assert {n.call_id for n in nodes} == {"root", "child"}
