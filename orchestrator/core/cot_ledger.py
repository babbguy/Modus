"""
Modus — Chain-of-Thought (CoT) Governance Ledger
=====================================================
Tamper-proof, hash-chained audit trail for governance decisions.

Records the reasoning behind every autonomous policy proposal, evolution
decision, and anomaly detection. Enables governance officers to audit
WHY the self-evolving engine made each decision.

Architecture mirrors poe_ledger.py:
  - SHA-256 hash chain per team
  - Bounded in-memory cache for chain continuity
  - Append-only (no updates, no deletes)
  - Offline-verifiable chain integrity
  - Stdlib only — no external dependencies
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

# ── Chain cache (bounded ring buffer per team) ────────────────────────────────

_CHAIN_CACHE: dict[str, deque] = {}  # team_id -> deque of (seq_num, entry_hash)
_MAX_CACHE_ENTRIES = 500


# ── Dataclass for hash computation ────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CoTChainEntry:
    """Immutable representation of a CoT ledger entry for hash computation."""
    team_id: str
    seq_num: int
    prev_hash: Optional[str]
    decision_type: str
    trigger: str
    decision_summary: str
    evidence_hash: str  # SHA-256 of the evidence_snapshot JSON

    def compute_hash(self) -> str:
        """Compute the entry hash. Deterministic for identical inputs."""
        payload = json.dumps({
            "team_id": self.team_id,
            "seq_num": self.seq_num,
            "prev_hash": self.prev_hash or "",
            "decision_type": self.decision_type,
            "trigger": self.trigger,
            "decision_summary": self.decision_summary,
            "evidence_hash": self.evidence_hash,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def hash_evidence(evidence: dict | None) -> str:
    """Hash an evidence snapshot for inclusion in the chain entry."""
    if not evidence:
        return hashlib.sha256(b"{}").hexdigest()
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# ── Chain state ───────────────────────────────────────────────────────────────

async def get_prev_hash(db: AsyncSession, team_id: str) -> tuple[int, Optional[str]]:
    """Get the previous hash and next sequence number for chain continuation."""
    # Check cache first
    if team_id in _CHAIN_CACHE and _CHAIN_CACHE[team_id]:
        last = _CHAIN_CACHE[team_id][-1]
        return last[0] + 1, last[1]

    # Fall back to DB
    from orchestrator.db.models import CoTLedgerEntry
    result = await db.execute(
        select(CoTLedgerEntry)
        .where(CoTLedgerEntry.team_id == team_id)
        .order_by(desc(CoTLedgerEntry.seq_num))
        .limit(1)
    )
    last_entry = result.scalar_one_or_none()
    if last_entry:
        return last_entry.seq_num + 1, last_entry.entry_hash
    return 0, None  # Genesis


def _update_cache(team_id: str, seq_num: int, entry_hash: str) -> None:
    """Update the in-memory chain cache (bounded ring buffer)."""
    if team_id not in _CHAIN_CACHE:
        _CHAIN_CACHE[team_id] = deque(maxlen=_MAX_CACHE_ENTRIES)
    _CHAIN_CACHE[team_id].append((seq_num, entry_hash))


# ── Append entry ──────────────────────────────────────────────────────────────

async def append_entry(
    db: AsyncSession,
    team_id: str,
    decision_type: str,
    trigger: str,
    decision_summary: str,
    evidence_snapshot: dict | None = None,
    rules_evaluated: list | None = None,
    reasoning_steps: list | None = None,
    alternatives_considered: list | None = None,
    linked_proposal_id: str | None = None,
    linked_policy_id: str | None = None,
    linked_evolution_gen_id: str | None = None,
    regulatory_tags: list | None = None,
) -> dict:
    """
    Append a new entry to the CoT hash chain for a team.

    Returns the entry dict including the computed entry_hash and id.
    This is an append-only operation — the entry cannot be modified after creation.
    """
    if not settings.cot_ledger_enabled:
        return {}

    from orchestrator.db.models import CoTLedgerEntry
    import uuid

    seq_num, prev_hash = await get_prev_hash(db, team_id)

    # Compute the chain hash
    ev_hash = hash_evidence(evidence_snapshot)
    chain_entry = CoTChainEntry(
        team_id=team_id,
        seq_num=seq_num,
        prev_hash=prev_hash,
        decision_type=decision_type,
        trigger=trigger,
        decision_summary=decision_summary,
        evidence_hash=ev_hash,
    )
    entry_hash = chain_entry.compute_hash()

    # Create DB record
    entry_id = str(uuid.uuid4())
    record = CoTLedgerEntry(
        id=entry_id,
        team_id=team_id,
        seq_num=seq_num,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        decision_type=decision_type,
        trigger=trigger,
        decision_summary=decision_summary,
        evidence_snapshot=evidence_snapshot,
        rules_evaluated=rules_evaluated,
        reasoning_steps=reasoning_steps,
        alternatives_considered=alternatives_considered,
        linked_proposal_id=linked_proposal_id,
        linked_policy_id=linked_policy_id,
        linked_evolution_gen_id=linked_evolution_gen_id,
        regulatory_tags=regulatory_tags,
    )
    db.add(record)

    # Update cache
    _update_cache(team_id, seq_num, entry_hash)

    logger.info(
        "CoT ledger entry #%d for team %s: [%s] %s",
        seq_num, team_id[:8], decision_type, decision_summary[:80],
    )

    return {
        "id": entry_id,
        "team_id": team_id,
        "seq_num": seq_num,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
        "decision_type": decision_type,
        "trigger": trigger,
        "decision_summary": decision_summary,
    }


# ── Link entry to proposal (post-flush) ──────────────────────────────────────

async def link_entry_to_proposal(
    db: AsyncSession,
    entry_id: str,
    proposal_id: str,
    policy_id: str | None = None,
) -> None:
    """Link a CoT entry to a created proposal/policy after DB flush."""
    if not entry_id:
        return
    from orchestrator.db.models import CoTLedgerEntry
    result = await db.execute(
        select(CoTLedgerEntry).where(CoTLedgerEntry.id == entry_id)
    )
    entry = result.scalar_one_or_none()
    if entry:
        entry.linked_proposal_id = proposal_id
        if policy_id:
            entry.linked_policy_id = policy_id


# ── Chain verification ────────────────────────────────────────────────────────

async def verify_chain(
    db: AsyncSession,
    team_id: str,
    start_seq: int = 0,
    end_seq: int | None = None,
) -> dict:
    """
    Verify the hash chain integrity for a team.

    Returns:
        {
            "valid": bool,
            "entries_checked": int,
            "first_invalid_seq": int | None,
            "error": str | None,
        }
    """
    from orchestrator.db.models import CoTLedgerEntry

    query = (
        select(CoTLedgerEntry)
        .where(CoTLedgerEntry.team_id == team_id)
        .where(CoTLedgerEntry.seq_num >= start_seq)
    )
    if end_seq is not None:
        query = query.where(CoTLedgerEntry.seq_num <= end_seq)
    query = query.order_by(CoTLedgerEntry.seq_num)

    result = await db.execute(query)
    entries = result.scalars().all()

    if not entries:
        return {"valid": True, "entries_checked": 0, "first_invalid_seq": None, "error": None}

    prev_hash = None
    for entry in entries:
        # Verify prev_hash linkage
        if entry.seq_num == 0:
            if entry.prev_hash is not None:
                return {
                    "valid": False,
                    "entries_checked": entry.seq_num + 1,
                    "first_invalid_seq": entry.seq_num,
                    "error": "Genesis entry has non-null prev_hash",
                }
        elif entry.prev_hash != prev_hash:
            return {
                "valid": False,
                "entries_checked": entry.seq_num + 1,
                "first_invalid_seq": entry.seq_num,
                "error": f"Chain break at seq {entry.seq_num}: expected prev_hash {prev_hash}, got {entry.prev_hash}",
            }

        # Recompute and verify entry hash
        ev_hash = hash_evidence(entry.evidence_snapshot)
        chain_entry = CoTChainEntry(
            team_id=entry.team_id,
            seq_num=entry.seq_num,
            prev_hash=entry.prev_hash,
            decision_type=entry.decision_type,
            trigger=entry.trigger,
            decision_summary=entry.decision_summary,
            evidence_hash=ev_hash,
        )
        expected_hash = chain_entry.compute_hash()
        if entry.entry_hash != expected_hash:
            return {
                "valid": False,
                "entries_checked": entry.seq_num + 1,
                "first_invalid_seq": entry.seq_num,
                "error": f"Hash mismatch at seq {entry.seq_num}: expected {expected_hash}, got {entry.entry_hash}",
            }

        prev_hash = entry.entry_hash

    return {
        "valid": True,
        "entries_checked": len(entries),
        "first_invalid_seq": None,
        "error": None,
    }


# ── Regulatory tag inference ──────────────────────────────────────────────────

REGULATORY_TAG_MAP = {
    "model_overprovision": ["nist_ai_rmf_govern_1"],
    "budget_overruns": ["nist_ai_rmf_govern_1", "eu_ai_act_article_9"],
    "amplification_patterns": ["eu_ai_act_article_14", "nist_ai_rmf_measure_2"],
    "rewind_patterns": ["eu_ai_act_article_14", "nist_ai_rmf_manage_2"],
    "iso42001_gaps": ["iso_42001", "eu_ai_act_article_17"],
    "pqc_migration": ["nist_pqc", "eu_ai_act_article_15"],
    "threshold_ceiling": ["nist_ai_rmf_govern_1"],
    "model_trigger_pattern": ["eu_ai_act_article_14"],
    "team_spend_anomaly": ["nist_ai_rmf_measure_2"],
    "content_policy_pattern": ["eu_ai_act_article_14", "eu_ai_act_article_10"],
    "evolution_proposal": ["eu_ai_act_article_14", "eu_ai_act_article_9"],
}


def infer_regulatory_tags(rule_name: str) -> list[str]:
    """Map a detection rule or decision type to relevant regulatory frameworks."""
    return REGULATORY_TAG_MAP.get(rule_name, [])


# ── Reasoning step builder ────────────────────────────────────────────────────

def build_reasoning_step(
    rule_name: str,
    observation: str,
    threshold: str | None = None,
    result: str | None = None,
    confidence: float = 1.0,
) -> dict:
    """Build a structured reasoning step for the CoT chain."""
    step: dict = {
        "rule": rule_name,
        "observation": observation,
    }
    if threshold:
        step["threshold"] = threshold
    if result:
        step["result"] = result
    if confidence < 1.0:
        step["confidence"] = round(confidence, 3)
    return step
