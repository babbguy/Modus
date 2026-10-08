"""
Modus — Chain-of-Thought Governance Ledger API
==================================================
REST endpoints for the CoT Ledger: list, detail, verify, export.

GET  /api/v1/governance/cot-ledger/entries          — paginated list
GET  /api/v1/governance/cot-ledger/entries/{id}      — single entry
GET  /api/v1/governance/cot-ledger/verify            — verify chain integrity
GET  /api/v1/governance/cot-ledger/export             — CSV/JSON export
GET  /api/v1/governance/cot-ledger/stats              — summary statistics
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import get_identity, Identity
from orchestrator.db.models import CoTLedgerEntry
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/governance/cot-ledger")


# ── Response schemas ──────────────────────────────────────────────────────────

class CoTEntryResponse(BaseModel):
    id: str
    team_id: str
    seq_num: int
    prev_hash: Optional[str]
    entry_hash: str
    decision_type: str
    trigger: str
    decision_summary: str
    evidence_snapshot: Optional[dict]
    rules_evaluated: Optional[list]
    reasoning_steps: Optional[list]
    alternatives_considered: Optional[list]
    linked_proposal_id: Optional[str]
    linked_policy_id: Optional[str]
    linked_evolution_gen_id: Optional[str]
    regulatory_tags: Optional[list]
    created_at: datetime

    class Config:
        from_attributes = True


class CoTStatsResponse(BaseModel):
    total_entries: int
    entries_by_type: dict
    entries_by_trigger: dict
    latest_entry_at: Optional[datetime]
    chain_length: int
    oldest_entry_at: Optional[datetime]


class ChainVerifyResponse(BaseModel):
    valid: bool
    entries_checked: int
    first_invalid_seq: Optional[int]
    error: Optional[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/entries", response_model=list[CoTEntryResponse])
async def list_entries(
    team_id: Optional[str] = None,
    decision_type: Optional[str] = None,
    trigger: Optional[str] = None,
    regulatory_tag: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[CoTEntryResponse]:
    """List CoT Ledger entries with optional filters."""
    query = select(CoTLedgerEntry).order_by(desc(CoTLedgerEntry.created_at))

    if team_id:
        identity.assert_team_access(team_id)
        query = query.where(CoTLedgerEntry.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        query = query.where(CoTLedgerEntry.team_id.in_(identity.team_ids))

    if decision_type:
        query = query.where(CoTLedgerEntry.decision_type == decision_type)
    if trigger:
        query = query.where(CoTLedgerEntry.trigger == trigger)
    if date_from:
        query = query.where(CoTLedgerEntry.created_at >= date_from)
    if date_to:
        query = query.where(CoTLedgerEntry.created_at <= date_to)

    # Regulatory tag filter (JSON array contains)
    if regulatory_tag:
        # SQLite: use json_each; PostgreSQL: use @> operator
        # For compatibility, filter in Python after fetch
        pass  # Handled post-query below

    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    entries = result.scalars().all()

    # Post-filter by regulatory tag if specified
    if regulatory_tag:
        entries = [
            e for e in entries
            if e.regulatory_tags and regulatory_tag in e.regulatory_tags
        ]

    return entries


@router.get("/entries/{entry_id}", response_model=CoTEntryResponse)
async def get_entry(
    entry_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> CoTEntryResponse:
    """Get a single CoT Ledger entry by ID."""
    entry = await db.get(CoTLedgerEntry, entry_id)
    if not entry:
        raise HTTPException(404, "CoT Ledger entry not found")
    if not identity.is_platform_admin and not identity.can_access_team(entry.team_id):
        raise HTTPException(404, "CoT Ledger entry not found")
    return entry


@router.get("/verify", response_model=ChainVerifyResponse)
async def verify_chain(
    team_id: Optional[str] = None,
    start_seq: int = Query(0, ge=0),
    end_seq: Optional[int] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> ChainVerifyResponse:
    """Verify the hash chain integrity for a team."""
    from orchestrator.core.cot_ledger import verify_chain as _verify

    # If no team_id, use the first team the user has access to
    if not team_id:
        if identity.team_ids:
            team_id = identity.team_ids[0]
        else:
            return ChainVerifyResponse(valid=True, entries_checked=0, first_invalid_seq=None, error=None)

    identity.assert_team_access(team_id)
    result = await _verify(db, team_id, start_seq, end_seq)
    return ChainVerifyResponse(**result)


@router.get("/stats", response_model=CoTStatsResponse)
async def get_stats(
    team_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> CoTStatsResponse:
    """Get summary statistics for the CoT Ledger."""
    base_filter = []
    if team_id:
        identity.assert_team_access(team_id)
        base_filter.append(CoTLedgerEntry.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        base_filter.append(CoTLedgerEntry.team_id.in_(identity.team_ids))

    # Total count
    total = await db.execute(
        select(func.count(CoTLedgerEntry.id)).where(*base_filter)
    )
    total_count = total.scalar_one()

    # By type
    type_result = await db.execute(
        select(CoTLedgerEntry.decision_type, func.count(CoTLedgerEntry.id))
        .where(*base_filter)
        .group_by(CoTLedgerEntry.decision_type)
    )
    entries_by_type = {r[0]: r[1] for r in type_result.all()}

    # By trigger
    trigger_result = await db.execute(
        select(CoTLedgerEntry.trigger, func.count(CoTLedgerEntry.id))
        .where(*base_filter)
        .group_by(CoTLedgerEntry.trigger)
    )
    entries_by_trigger = {r[0]: r[1] for r in trigger_result.all()}

    # Latest and oldest
    time_result = await db.execute(
        select(
            func.max(CoTLedgerEntry.created_at),
            func.min(CoTLedgerEntry.created_at),
            func.max(CoTLedgerEntry.seq_num),
        ).where(*base_filter)
    )
    time_row = time_result.one()

    return CoTStatsResponse(
        total_entries=total_count,
        entries_by_type=entries_by_type,
        entries_by_trigger=entries_by_trigger,
        latest_entry_at=time_row[0],
        oldest_entry_at=time_row[1],
        chain_length=(time_row[2] or 0) + 1 if time_row[2] is not None else 0,
    )


@router.get("/export")
async def export_entries(
    team_id: Optional[str] = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = Query(1000, ge=1, le=10000),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Export CoT Ledger entries as JSON or CSV."""
    query = select(CoTLedgerEntry).order_by(CoTLedgerEntry.seq_num)

    if team_id:
        identity.assert_team_access(team_id)
        query = query.where(CoTLedgerEntry.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        query = query.where(CoTLedgerEntry.team_id.in_(identity.team_ids))

    if date_from:
        query = query.where(CoTLedgerEntry.created_at >= date_from)
    if date_to:
        query = query.where(CoTLedgerEntry.created_at <= date_to)

    query = query.limit(limit)
    result = await db.execute(query)
    entries = result.scalars().all()

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "seq_num", "entry_hash", "prev_hash", "decision_type", "trigger",
            "decision_summary", "evidence_snapshot", "rules_evaluated",
            "reasoning_steps", "alternatives_considered", "regulatory_tags",
            "linked_proposal_id", "linked_policy_id", "created_at",
        ])
        for e in entries:
            writer.writerow([
                e.seq_num, e.entry_hash, e.prev_hash or "", e.decision_type,
                e.trigger, e.decision_summary,
                json.dumps(e.evidence_snapshot) if e.evidence_snapshot else "",
                json.dumps(e.rules_evaluated) if e.rules_evaluated else "",
                json.dumps(e.reasoning_steps) if e.reasoning_steps else "",
                json.dumps(e.alternatives_considered) if e.alternatives_considered else "",
                json.dumps(e.regulatory_tags) if e.regulatory_tags else "",
                e.linked_proposal_id or "", e.linked_policy_id or "",
                e.created_at.isoformat() if e.created_at else "",
            ])
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=cot-ledger-export.csv"},
        )

    # JSON format
    data = [
        {
            "seq_num": e.seq_num,
            "entry_hash": e.entry_hash,
            "prev_hash": e.prev_hash,
            "decision_type": e.decision_type,
            "trigger": e.trigger,
            "decision_summary": e.decision_summary,
            "evidence_snapshot": e.evidence_snapshot,
            "rules_evaluated": e.rules_evaluated,
            "reasoning_steps": e.reasoning_steps,
            "alternatives_considered": e.alternatives_considered,
            "regulatory_tags": e.regulatory_tags,
            "linked_proposal_id": e.linked_proposal_id,
            "linked_policy_id": e.linked_policy_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return StreamingResponse(
        io.BytesIO(json.dumps(data, indent=2).encode()),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=cot-ledger-export.json"},
    )
