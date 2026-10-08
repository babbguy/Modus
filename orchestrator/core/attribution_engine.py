"""
Modus — Attribution Engine (Phase 5)
============================================
Warm-path processor for agentic cost attribution.

Runs as a background task every 30 seconds. Picks up completed sessions
(no new usage records for SESSION_TIMEOUT seconds), builds the call graph,
computes Tree Shapley attribution, and writes results to DB.

Algorithm:
    1. Find sessions with recent usage records but no processed attribution
    2. For each completed session, extract span data from usage_record metadata
    3. Build directed tree from call_id / parent_call_id relationships
    4. Detect retries (consecutive calls with same parent + model)
    5. Detect defensive spend (fallback calls after a retry)
    6. Compute Tree Shapley attribution in O(n) bottom-up pass
    7. Calculate amplification factors, retry tax, defensive spend
    8. Persist AttributionSession + AttributionNode rows

No external dependencies. Pure Python stdlib graph algorithms.
Works on SQLite and PostgreSQL.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from orchestrator.db.models import (
    AttributionNode, AttributionSession, UsageRecord,
)
from orchestrator.db.session import _session_factory

logger = logging.getLogger(__name__)

# A session is "complete" when no new records arrive for this many seconds
SESSION_TIMEOUT = 120
# Maximum sessions to process per cycle (prevent long-running transactions)
MAX_SESSIONS_PER_CYCLE = 20


async def process_pending_sessions() -> int:
    """
    Main entry point. Find completed sessions, compute attribution, persist.
    Returns the number of sessions processed.
    """
    if _session_factory is None:
        return 0

    processed = 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=SESSION_TIMEOUT)

    async with _session_factory() as db:
        # Find sessions with records that have span metadata and are "complete"
        # (last record older than SESSION_TIMEOUT) but not yet attributed
        result = await db.execute(
            select(
                UsageRecord.session_id,
                func.count(UsageRecord.id).label("record_count"),
                func.max(UsageRecord.timestamp).label("last_seen"),
                func.min(UsageRecord.timestamp).label("first_seen"),
            ).where(
                UsageRecord.session_id.isnot(None),
                UsageRecord.session_id != "",
            ).group_by(
                UsageRecord.session_id,
            ).having(
                func.max(UsageRecord.timestamp) < cutoff,
            ).limit(MAX_SESSIONS_PER_CYCLE)
        )
        candidate_sessions = result.all()

        for row in candidate_sessions:
            session_id = row.session_id
            # Skip if already processed
            existing = (await db.execute(
                select(AttributionSession.id).where(
                    AttributionSession.session_id == session_id,
                    AttributionSession.status == "processed",
                )
            )).scalar_one_or_none()
            if existing:
                continue

            try:
                await _process_session(db, session_id, row.first_seen, row.last_seen)
                processed += 1
            except Exception:
                logger.error("Attribution failed for session %s", session_id, exc_info=True)

        if processed:
            await db.commit()

    if processed:
        logger.info("Attribution engine processed %d sessions", processed)
    return processed


async def _process_session(
    db, session_id: str,
    first_seen: datetime, last_seen: datetime,
) -> None:
    """Process a single session: build graph, compute attribution, persist."""
    # Fetch all usage records for this session
    records = (await db.execute(
        select(UsageRecord).where(
            UsageRecord.session_id == session_id,
        ).order_by(UsageRecord.timestamp)
    )).scalars().all()

    if not records:
        return

    # Extract span data from metadata
    spans = []
    for rec in records:
        # The JSON column is mapped as ``metadata_``; ``rec.metadata`` is the
        # declarative base's MetaData object, not the record's data.
        meta = rec.metadata_ or {}
        call_id = meta.get("mds_call_id")
        if not call_id:
            # Generate a synthetic call_id for records without span data
            call_id = f"syn_{str(rec.id)[:12]}"

        spans.append({
            "call_id": call_id,
            "parent_call_id": meta.get("mds_parent_id"),
            "session_id": session_id,
            "provider": rec.provider,
            "model": rec.model,
            "input_tokens": rec.input_tokens or 0,
            "output_tokens": rec.output_tokens or 0,
            "total_cost": Decimal(str(rec.total_cost or 0)),
            "duration_ms": rec.duration_ms or 0,
            "timestamp": rec.timestamp,
            "app_id": str(rec.app_id),
            "team_id": str(rec.team_id),
            "span_name": meta.get("mds_span_name"),
            "fw_tier": meta.get("mds_fw_tier", "custom"),
        })

    if not spans:
        return

    app_id = spans[0]["app_id"]
    team_id = spans[0]["team_id"]
    fw_tier = spans[0].get("fw_tier", "custom")

    # Build the call graph
    graph = _build_graph(spans)

    # Detect retries and defensive spend
    _detect_retries(graph, spans)
    _detect_defensive(graph, spans)

    # Compute Tree Shapley attribution
    _compute_attribution(graph, spans)

    # Calculate session-level metrics
    total_cost = sum(s["total_cost"] for s in spans)
    retry_cost = sum(s["total_cost"] for s in spans if s.get("is_retry"))
    defensive_cost = sum(s["total_cost"] for s in spans if s.get("is_defensive"))
    total_input = sum(s["input_tokens"] for s in spans)
    total_output = sum(s["output_tokens"] for s in spans)

    # Confidence based on framework tier and data quality
    has_parent_chains = any(s["parent_call_id"] for s in spans)
    if fw_tier == "structured" and has_parent_chains:
        confidence = 0.90
    elif fw_tier == "structured":
        confidence = 0.75
    elif has_parent_chains:
        confidence = 0.70
    else:
        confidence = 0.50

    # Build graph JSON for visualization (span waterfall)
    graph_json = _build_graph_json(spans, graph)

    # Upsert AttributionSession
    existing_session = (await db.execute(
        select(AttributionSession).where(
            AttributionSession.session_id == session_id
        )
    )).scalar_one_or_none()

    if existing_session:
        attr_session = existing_session
        attr_session.status = "processed"
        attr_session.total_cost = total_cost
        attr_session.total_calls = len(spans)
        attr_session.total_input_tokens = total_input
        attr_session.total_output_tokens = total_output
        attr_session.retry_cost = retry_cost
        attr_session.defensive_cost = defensive_cost
        attr_session.attribution_confidence = confidence
        attr_session.graph_json = graph_json
        attr_session.processed_at = datetime.now(timezone.utc)
    else:
        attr_session = AttributionSession(
            session_id=session_id,
            app_id=app_id,
            team_id=team_id,
            framework_tier=fw_tier,
            status="processed",
            total_cost=total_cost,
            total_calls=len(spans),
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            retry_cost=retry_cost,
            defensive_cost=defensive_cost,
            attribution_confidence=confidence,
            graph_json=graph_json,
            started_at=first_seen,
            ended_at=last_seen,
            processed_at=datetime.now(timezone.utc),
        )
        db.add(attr_session)

    # Write attribution nodes
    for span in spans:
        node_label = span.get("span_name") or f"{span['provider']}/{span['model'] or '?'}"
        db.add(AttributionNode(
            session_id=session_id,
            call_id=span["call_id"],
            parent_call_id=span["parent_call_id"],
            node_label=node_label,
            provider=span["provider"],
            model=span["model"],
            direct_cost=span["total_cost"],
            attributed_cost=span.get("attributed_cost", span["total_cost"]),
            amplification_factor=span.get("amplification_factor"),
            retry_tax=span.get("retry_tax", Decimal("0")),
            defensive_spend=span.get("defensive_spend", Decimal("0")),
            confidence=confidence,
            input_tokens=span["input_tokens"],
            output_tokens=span["output_tokens"],
            call_count=1,
            is_retry=span.get("is_retry", False),
            is_defensive=span.get("is_defensive", False),
            app_id=app_id,
            team_id=team_id,
        ))


def _build_graph(spans: list[dict]) -> dict:
    """
    Build adjacency lists from span data.
    Returns {call_id: {"children": [call_id, ...], "parent": call_id|None}}
    """
    graph: dict[str, dict] = {}
    for s in spans:
        cid = s["call_id"]
        if cid not in graph:
            graph[cid] = {"children": [], "parent": s["parent_call_id"]}
        else:
            graph[cid]["parent"] = s["parent_call_id"]

    # Build children lists
    for cid, node in graph.items():
        pid = node["parent"]
        if pid and pid in graph:
            graph[pid]["children"].append(cid)

    return graph


def _detect_retries(graph: dict, spans: list[dict]) -> None:
    """
    Identify retry calls: consecutive calls from the same parent
    with the same provider+model. Mark is_retry=True on the span.
    Attribute retry cost to the node that caused the retry (parent).
    """
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s["parent_call_id"]:
            by_parent[s["parent_call_id"]].append(s)

    for parent_id, children in by_parent.items():
        # Sort by timestamp
        children.sort(key=lambda x: x["timestamp"])

        # Group consecutive calls with same provider+model
        seen_signatures: dict[str, int] = {}
        for child in children:
            sig = f"{child['provider']}:{child['model']}"
            count = seen_signatures.get(sig, 0)
            if count > 0:
                child["is_retry"] = True
                child["retry_of_call_id"] = children[0]["call_id"]
            seen_signatures[sig] = count + 1


def _detect_defensive(graph: dict, spans: list[dict]) -> None:
    """
    Identify defensive spend: when a parent has a retry child followed
    by a child with a different model, the different-model child is
    a fallback (defensive spend).
    """
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s["parent_call_id"]:
            by_parent[s["parent_call_id"]].append(s)

    for parent_id, children in by_parent.items():
        has_retry = any(c.get("is_retry") for c in children)
        if not has_retry:
            continue

        # Children after the retry with a different model are defensive
        retry_models = {c["model"] for c in children if c.get("is_retry")}
        for child in children:
            if not child.get("is_retry") and child["model"] not in retry_models:
                # This child uses a different model after retries — likely a fallback
                child["is_defensive"] = True


def _compute_attribution(graph: dict, spans: list[dict]) -> None:
    """
    Tree Shapley attribution: O(n) bottom-up pass.

    Each node's attributed_cost = direct_cost + proportional share of
    children's attributed costs (weighted by each child's direct cost
    contribution to the total children cost).

    Amplification factor = total_downstream_cost / direct_cost.
    """
    # Build lookup
    span_by_id = {s["call_id"]: s for s in spans}

    # Find root nodes (no parent or parent not in graph)
    roots = [s["call_id"] for s in spans
             if not s["parent_call_id"] or s["parent_call_id"] not in span_by_id]

    # Bottom-up pass using post-order traversal
    visited = set()
    order = []

    def _post_order(cid: str):
        if cid in visited:
            return
        visited.add(cid)
        if cid in graph:
            for child_id in graph[cid]["children"]:
                if child_id in span_by_id:
                    _post_order(child_id)
        order.append(cid)

    for root in roots:
        _post_order(root)

    # Compute attributed costs bottom-up
    for cid in order:
        span = span_by_id.get(cid)
        if not span:
            continue

        direct = span["total_cost"]
        children_ids = graph.get(cid, {}).get("children", [])
        children_cost = sum(
            span_by_id[c].get("attributed_cost", span_by_id[c]["total_cost"])
            for c in children_ids if c in span_by_id
        )

        span["attributed_cost"] = direct + children_cost

        # Amplification factor
        if direct > 0:
            span["amplification_factor"] = float(children_cost / direct)
        else:
            span["amplification_factor"] = 0.0

    # Compute retry tax: attribute retry cost to the parent that caused it
    for span in spans:
        parent_id = span["parent_call_id"]
        if span.get("is_retry") and parent_id and parent_id in span_by_id:
            parent = span_by_id[parent_id]
            parent["retry_tax"] = parent.get("retry_tax", Decimal("0")) + span["total_cost"]

    # Compute defensive spend: attribute fallback cost to the retry node's parent
    for span in spans:
        parent_id = span["parent_call_id"]
        if span.get("is_defensive") and parent_id and parent_id in span_by_id:
            parent = span_by_id[parent_id]
            parent["defensive_spend"] = parent.get("defensive_spend", Decimal("0")) + span["total_cost"]


def _build_graph_json(spans: list[dict], graph: dict) -> dict:
    """
    Build a serializable graph structure for trace visualization.
    Returns a dict with nodes and edges for the dashboard span waterfall.
    """
    nodes = []
    edges = []

    for s in spans:
        nodes.append({
            "id": s["call_id"],
            "label": s.get("span_name") or f"{s['provider']}/{s['model'] or '?'}",
            "provider": s["provider"],
            "model": s["model"],
            "input_tokens": s["input_tokens"],
            "output_tokens": s["output_tokens"],
            "cost": str(s["total_cost"]),
            "duration_ms": s["duration_ms"],
            "is_retry": s.get("is_retry", False),
            "is_defensive": s.get("is_defensive", False),
            "amplification_factor": s.get("amplification_factor", 0),
            "attributed_cost": str(s.get("attributed_cost", s["total_cost"])),
        })

        if s["parent_call_id"]:
            edges.append({
                "from": s["parent_call_id"],
                "to": s["call_id"],
            })

    return {"nodes": nodes, "edges": edges}
