"""
Modus — Attribution API (Phase 5)
=========================================
Endpoints for agentic cost attribution analytics and deep trace visualization.

GET  /api/v1/attribution/sessions                — list sessions with attribution summary
GET  /api/v1/attribution/sessions/{session_id}   — full session detail + graph
GET  /api/v1/attribution/amplification           — top nodes by amplification factor
GET  /api/v1/attribution/retry-tax               — retry tax report
GET  /api/v1/attribution/defensive-spend         — defensive spend breakdown
GET  /api/v1/attribution/trace/{session_id}      — deep trace data for visualization
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import AttributionNode, AttributionSession
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attribution", tags=["attribution"])


# ── Response schemas ──────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    session_id: str
    app_id: str
    team_id: str
    framework_tier: str
    total_cost: str  # Decimal as string for precision
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    retry_cost: str  # Decimal as string for precision
    defensive_cost: str  # Decimal as string for precision
    attribution_confidence: Optional[float]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]


class NodeDetail(BaseModel):
    call_id: str
    parent_call_id: Optional[str]
    node_label: str
    provider: Optional[str]
    model: Optional[str]
    direct_cost: str  # Decimal as string for precision
    attributed_cost: str  # Decimal as string for precision
    amplification_factor: Optional[float]
    retry_tax: str  # Decimal as string for precision
    defensive_spend: str  # Decimal as string for precision
    confidence: Optional[float]
    input_tokens: int
    output_tokens: int
    call_count: int
    is_retry: bool
    is_defensive: bool


class SessionDetail(BaseModel):
    session_id: str
    app_id: str
    team_id: str
    framework_tier: str
    total_cost: str  # Decimal as string for precision
    total_calls: int
    retry_cost: str  # Decimal as string for precision
    defensive_cost: str  # Decimal as string for precision
    attribution_confidence: Optional[float]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    nodes: list[NodeDetail]
    graph: Optional[dict] = None


class AmplificationEntry(BaseModel):
    session_id: str
    call_id: str
    node_label: str
    provider: Optional[str]
    model: Optional[str]
    direct_cost: str  # Decimal as string for precision
    attributed_cost: str  # Decimal as string for precision
    amplification_factor: float
    app_id: str


class RetryTaxEntry(BaseModel):
    session_id: str
    call_id: str
    node_label: str
    retry_tax: str  # Decimal as string for precision
    direct_cost: str  # Decimal as string for precision
    retry_tax_pct: str  # Decimal as string for precision
    app_id: str


class DefensiveSpendEntry(BaseModel):
    session_id: str
    call_id: str
    node_label: str
    defensive_spend: str  # Decimal as string for precision
    direct_cost: str  # Decimal as string for precision
    is_defensive: bool
    app_id: str


# ── GET /attribution/sessions ────────────────────────────────────────────────

@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(
    hours: int = Query(24, ge=1, le=720),
    app_id: Optional[str] = None,
    min_cost: float = Query(0.0, ge=0),
    framework_tier: Optional[str] = Query(None, pattern="^(structured|custom)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[SessionSummary]:
    """List attributed sessions with summary metrics."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = select(AttributionSession).where(
        AttributionSession.status == "processed",
        AttributionSession.started_at >= since,
    )

    if not identity.is_platform_admin:
        q = q.where(AttributionSession.team_id == identity.team_id)
    if app_id:
        q = q.where(AttributionSession.app_id == app_id)
    if min_cost > 0:
        q = q.where(AttributionSession.total_cost >= Decimal(str(min_cost)))
    if framework_tier:
        q = q.where(AttributionSession.framework_tier == framework_tier)

    q = q.order_by(AttributionSession.started_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(q)).scalars().all()

    return [SessionSummary(
        session_id=r.session_id,
        app_id=str(r.app_id),
        team_id=str(r.team_id),
        framework_tier=r.framework_tier,
        total_cost=str(r.total_cost),
        total_calls=r.total_calls,
        total_input_tokens=r.total_input_tokens,
        total_output_tokens=r.total_output_tokens,
        retry_cost=str(r.retry_cost),
        defensive_cost=str(r.defensive_cost),
        attribution_confidence=r.attribution_confidence,
        started_at=r.started_at,
        ended_at=r.ended_at,
    ) for r in rows]


# ── GET /attribution/sessions/{session_id} ───────────────────────────────────

@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> SessionDetail:
    """Full session detail with all nodes and the call graph."""
    sess = (await db.execute(
        select(AttributionSession).where(
            AttributionSession.session_id == session_id,
        )
    )).scalar_one_or_none()

    if not sess:
        raise HTTPException(404, "Session not found.")

    if not identity.is_platform_admin:
        identity.assert_team_access(str(sess.team_id))

    nodes = (await db.execute(
        select(AttributionNode).where(
            AttributionNode.session_id == session_id,
        ).order_by(AttributionNode.created_at)
    )).scalars().all()

    return SessionDetail(
        session_id=sess.session_id,
        app_id=str(sess.app_id),
        team_id=str(sess.team_id),
        framework_tier=sess.framework_tier,
        total_cost=str(sess.total_cost),
        total_calls=sess.total_calls,
        retry_cost=str(sess.retry_cost),
        defensive_cost=str(sess.defensive_cost),
        attribution_confidence=sess.attribution_confidence,
        started_at=sess.started_at,
        ended_at=sess.ended_at,
        nodes=[NodeDetail(
            call_id=n.call_id,
            parent_call_id=n.parent_call_id,
            node_label=n.node_label,
            provider=n.provider,
            model=n.model,
            direct_cost=str(n.direct_cost),
            attributed_cost=str(n.attributed_cost),
            amplification_factor=n.amplification_factor,
            retry_tax=str(n.retry_tax),
            defensive_spend=str(n.defensive_spend),
            confidence=n.confidence,
            input_tokens=n.input_tokens,
            output_tokens=n.output_tokens,
            call_count=n.call_count,
            is_retry=n.is_retry,
            is_defensive=n.is_defensive,
        ) for n in nodes],
        graph=sess.graph_json,
    )


# ── GET /attribution/amplification ───────────────────────────────────────────

@router.get("/amplification", response_model=list[AmplificationEntry])
async def top_amplification(
    hours: int = Query(24, ge=1, le=720),
    min_af: float = Query(2.0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[AmplificationEntry]:
    """
    Top nodes by amplification factor across all sessions.
    High AF = optimization target: node whose output verbosity drives downstream costs.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = select(AttributionNode).where(
        AttributionNode.amplification_factor >= min_af,
        AttributionNode.created_at >= since,
    )
    if not identity.is_platform_admin:
        q = q.where(AttributionNode.team_id == identity.team_id)

    q = q.order_by(AttributionNode.amplification_factor.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    return [AmplificationEntry(
        session_id=r.session_id,
        call_id=r.call_id,
        node_label=r.node_label,
        provider=r.provider,
        model=r.model,
        direct_cost=str(r.direct_cost),
        attributed_cost=str(r.attributed_cost),
        amplification_factor=r.amplification_factor or 0,
        app_id=str(r.app_id),
    ) for r in rows]


# ── GET /attribution/retry-tax ───────────────────────────────────────────────

@router.get("/retry-tax", response_model=list[RetryTaxEntry])
async def retry_tax_report(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[RetryTaxEntry]:
    """
    Nodes with highest retry tax — cost of retries attributed to the node
    that caused them (not the retrying node).
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = select(AttributionNode).where(
        AttributionNode.retry_tax > 0,
        AttributionNode.created_at >= since,
    )
    if not identity.is_platform_admin:
        q = q.where(AttributionNode.team_id == identity.team_id)

    q = q.order_by(AttributionNode.retry_tax.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    return [RetryTaxEntry(
        session_id=r.session_id,
        call_id=r.call_id,
        node_label=r.node_label,
        retry_tax=str(r.retry_tax),
        direct_cost=str(r.direct_cost),
        retry_tax_pct=str(r.retry_tax / r.direct_cost * 100) if r.direct_cost else "0",
        app_id=str(r.app_id),
    ) for r in rows]


# ── GET /attribution/defensive-spend ─────────────────────────────────────────

@router.get("/defensive-spend", response_model=list[DefensiveSpendEntry])
async def defensive_spend_report(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[DefensiveSpendEntry]:
    """
    Nodes with highest defensive spend — cost of fallback subgraphs
    triggered by earlier failures.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = select(AttributionNode).where(
        AttributionNode.defensive_spend > 0,
        AttributionNode.created_at >= since,
    )
    if not identity.is_platform_admin:
        q = q.where(AttributionNode.team_id == identity.team_id)

    q = q.order_by(AttributionNode.defensive_spend.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    return [DefensiveSpendEntry(
        session_id=r.session_id,
        call_id=r.call_id,
        node_label=r.node_label,
        defensive_spend=str(r.defensive_spend),
        direct_cost=str(r.direct_cost),
        is_defensive=r.is_defensive,
        app_id=str(r.app_id),
    ) for r in rows]


# ── GET /attribution/trace/{session_id} ──────────────────────────────────────

@router.get("/trace/{session_id}")
async def get_trace(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Deep trace data for visualization: span waterfall and call graph.
    Returns the graph_json from the attribution session plus timing data
    for rendering a span waterfall diagram.
    """
    sess = (await db.execute(
        select(AttributionSession).where(
            AttributionSession.session_id == session_id,
        )
    )).scalar_one_or_none()

    if not sess:
        raise HTTPException(404, "Session not found.")

    if not identity.is_platform_admin:
        identity.assert_team_access(str(sess.team_id))

    return {
        "session_id": sess.session_id,
        "framework_tier": sess.framework_tier,
        "attribution_confidence": sess.attribution_confidence,
        "total_cost": str(sess.total_cost),
        "total_calls": sess.total_calls,
        "retry_cost": str(sess.retry_cost),
        "defensive_cost": str(sess.defensive_cost),
        "started_at": sess.started_at.isoformat() if sess.started_at else None,
        "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
        "graph": sess.graph_json,
    }
