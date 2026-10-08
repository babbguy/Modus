"""
Modus — Proof-of-Enforcement Chained Audit Ledger
========================================================
Tamper-evident, hash-linked enforcement ledger with Merkle anchoring.

Tier 1: SHA-256 hash chain (stdlib only)
Tier 2: Optional EXPERIMENTAL ArithmeticCircuit value (proof_type "snark"/
        "aggregate_zk"). This is NOT a production zk-SNARK — the circuit has
        no real constraints and carries no soundness guarantee; the label is
        retained only as a stored proof_type value. See ``zk_circuit``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

# Ring buffer for recent chain state (bounded memory)
_CHAIN_CACHE: dict[str, deque] = {}  # team_id -> deque of (seq_num, entry_hash)
_MAX_CACHE_ENTRIES = 1000


@dataclass(frozen=True, slots=True)
class ChainEntry:
    """Immutable representation of a ledger entry."""
    team_id: str
    session_id: str
    seq_num: int
    prev_hash: Optional[str]
    decision_hash: str
    trajectory_proof_id: Optional[str]
    proof_type: str
    risk_level: str
    entry_hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps({
            "team_id": self.team_id,
            "session_id": self.session_id,
            "seq_num": self.seq_num,
            "prev_hash": self.prev_hash or "",
            "decision_hash": self.decision_hash,
            "proof_type": self.proof_type,
            "risk_level": self.risk_level,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def hash_decision(decision: str, reason: str, app_id: str, estimated_cost: float) -> str:
    """Hash an enforcement decision for inclusion in the ledger."""
    payload = json.dumps({
        "decision": decision,
        "reason": reason,
        "app_id": app_id,
        "estimated_cost": str(estimated_cost),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def should_sample(risk_level: str = "medium") -> bool:
    """Determine if this request should be recorded based on sampling config."""
    if not settings.poe_ledger_enabled:
        return False
    if settings.poe_high_risk_always_prove and risk_level == "high":
        return True
    return random.random() < settings.poe_sample_rate


async def get_prev_hash(db: AsyncSession, team_id: str) -> tuple[int, Optional[str]]:
    """Get the previous hash and next sequence number for chain continuation."""
    # Check cache first
    if team_id in _CHAIN_CACHE and _CHAIN_CACHE[team_id]:
        last = _CHAIN_CACHE[team_id][-1]
        return last[0] + 1, last[1]

    # Fall back to DB
    from orchestrator.db.models import PoELedgerEntry
    result = await db.execute(
        select(PoELedgerEntry)
        .where(PoELedgerEntry.team_id == team_id)
        .order_by(desc(PoELedgerEntry.seq_num))
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


async def append_entry(
    db: AsyncSession,
    team_id: str,
    session_id: str,
    decision_hash: str,
    risk_level: str = "medium",
    trajectory_proof_id: Optional[str] = None,
) -> dict:
    """Append a new entry to the hash chain. Returns the entry dict."""
    seq_num, prev_hash = await get_prev_hash(db, team_id)

    proof_type = "hash_chain"
    tee_quote = None
    tee_platform = None

    # Attempt TEE attestation if available
    try:
        from orchestrator.core.tee_attestation import generate_enclave_quote
        quote = generate_enclave_quote(decision_hash.encode())
        if quote:
            tee_quote = quote
            from orchestrator.core.tee_attestation import detect_tee_hardware
            tee_platform = detect_tee_hardware()
    except Exception:
        pass  # TEE is optional

    entry = ChainEntry(
        team_id=team_id,
        session_id=session_id,
        seq_num=seq_num,
        prev_hash=prev_hash,
        decision_hash=decision_hash,
        trajectory_proof_id=trajectory_proof_id,
        proof_type=proof_type,
        risk_level=risk_level,
    )
    entry_hash = entry.compute_hash()

    # Optional experimental ArithmeticCircuit value (Tier 2; not a real SNARK)
    snark_proof_type = proof_type
    if settings.poe_snark_enabled:
        try:
            snark_proof_type = _generate_snark_proof(entry, entry_hash)
        except Exception as exc:
            logger.debug("Tier-2 circuit value generation failed, using hash_chain: %s", exc)

    # Persist to DB
    from orchestrator.db.models import PoELedgerEntry
    import uuid as _uuid_mod
    record = PoELedgerEntry(
        id=str(_uuid_mod.uuid4()),
        team_id=team_id,
        session_id=session_id,
        seq_num=seq_num,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        decision_hash=decision_hash,
        trajectory_proof_id=trajectory_proof_id,
        proof_type=snark_proof_type,
        tee_quote=tee_quote,
        tee_platform=tee_platform,
        risk_level=risk_level,
    )
    db.add(record)

    _update_cache(team_id, seq_num, entry_hash)

    # Optional SLH-DSA post-quantum signature (Phase 10 B3)
    slh_dsa_sig = None
    if settings.pqc_slh_dsa_enabled:
        try:
            from orchestrator.core.attestation_engine import resolve_attestation_key
            att_key = resolve_attestation_key()
            if att_key:
                slh_dsa_sig = sign_entry_slh_dsa(entry_hash, att_key)
        except Exception as exc:
            logger.debug("SLH-DSA signing skipped: %s", exc)

    return {
        "team_id": team_id,
        "session_id": session_id,
        "seq_num": seq_num,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
        "decision_hash": decision_hash,
        "trajectory_proof_id": trajectory_proof_id,
        "proof_type": snark_proof_type,
        "tee_quote": tee_quote,
        "tee_platform": tee_platform,
        "risk_level": risk_level,
        "slh_dsa_signature": slh_dsa_sig,
    }


def _generate_snark_proof(entry: ChainEntry, entry_hash: str) -> str:
    """Build the EXPERIMENTAL Tier-2 circuit value for the chain entry.

    NOT a real zk-SNARK: it allocates inputs into an empty (constraint-free)
    ArithmeticCircuit, so ``check_constraints`` trivially passes, and returns
    the stored proof_type label ``"snark"``. Carries no soundness guarantee.
    """
    try:
        from orchestrator.core.zk_circuit import ArithmeticCircuit
        circuit = ArithmeticCircuit()

        # Encode hash prefix as field element
        hash_val = int(entry_hash[:8], 16)
        prev_val = int(entry.prev_hash[:8], 16) if entry.prev_hash else 0

        circuit.allocate_public_input("entry_hash", hash_val)
        circuit.allocate_public_input("prev_hash", prev_val)
        circuit.allocate_variable("seq_num", entry.seq_num)

        # Constraint: seq_num >= 0 (always true for unsigned)
        circuit.allocate_variable("zero", 0)

        if circuit.check_constraints():
            return "snark"
    except Exception:
        pass
    return "hash_chain"


async def verify_chain(db: AsyncSession, session_id: str) -> dict:
    """Verify the hash chain integrity for a session."""
    from orchestrator.db.models import PoELedgerEntry
    result = await db.execute(
        select(PoELedgerEntry)
        .where(PoELedgerEntry.session_id == session_id)
        .order_by(PoELedgerEntry.seq_num)
    )
    entries = result.scalars().all()

    if not entries:
        return {"valid": True, "entries": 0, "errors": []}

    errors = []
    for i, entry in enumerate(entries):
        # Recompute hash
        ce = ChainEntry(
            team_id=entry.team_id,
            session_id=entry.session_id,
            seq_num=entry.seq_num,
            prev_hash=entry.prev_hash,
            decision_hash=entry.decision_hash,
            trajectory_proof_id=str(entry.trajectory_proof_id) if entry.trajectory_proof_id else None,
            proof_type=entry.proof_type,
            risk_level=entry.risk_level,
        )
        expected = ce.compute_hash()
        if entry.entry_hash != expected:
            errors.append({
                "seq_num": entry.seq_num,
                "expected": expected,
                "actual": entry.entry_hash,
                "error": "hash_mismatch",
            })

        # Verify chain link
        if i > 0 and entry.prev_hash != entries[i - 1].entry_hash:
            errors.append({
                "seq_num": entry.seq_num,
                "expected_prev": entries[i - 1].entry_hash,
                "actual_prev": entry.prev_hash,
                "error": "chain_break",
            })

    return {
        "valid": len(errors) == 0,
        "entries": len(entries),
        "errors": errors,
    }


async def build_merkle_anchor(db: AsyncSession, team_id: str) -> Optional[dict]:
    """Build a Merkle tree from unanchored entries and create an anchor record."""
    from orchestrator.db.models import PoELedgerEntry, PoEMerkleAnchor
    from orchestrator.core.merkle import MerkleTree

    # Find the last anchor to know where to start
    last_anchor = await db.execute(
        select(PoEMerkleAnchor)
        .where(PoEMerkleAnchor.team_id == team_id)
        .order_by(desc(PoEMerkleAnchor.anchored_at))
        .limit(1)
    )
    last_anchor = last_anchor.scalar_one_or_none()

    # Get unanchored entries
    q = select(PoELedgerEntry).where(
        PoELedgerEntry.team_id == team_id
    ).order_by(PoELedgerEntry.seq_num)

    if last_anchor:
        q = q.where(PoELedgerEntry.created_at > last_anchor.anchored_at)

    result = await db.execute(q)
    entries = result.scalars().all()

    if not entries:
        return None

    # Build Merkle tree
    tree = MerkleTree()
    for entry in entries:
        tree.add_leaf(entry.entry_hash)

    root = tree.build()

    # Create anchor
    anchor = PoEMerkleAnchor(
        team_id=team_id,
        merkle_root=root,
        entry_count=len(entries),
        first_entry_id=entries[0].id,
        last_entry_id=entries[-1].id,
    )
    db.add(anchor)
    await db.flush()

    return {
        "merkle_root": root,
        "entry_count": len(entries),
        "team_id": team_id,
        "anchor_id": str(anchor.id),
    }


async def export_chain(db: AsyncSession, session_id: str) -> list[dict]:
    """Export chain entries for offline audit."""
    from orchestrator.db.models import PoELedgerEntry
    result = await db.execute(
        select(PoELedgerEntry)
        .where(PoELedgerEntry.session_id == session_id)
        .order_by(PoELedgerEntry.seq_num)
    )
    entries = result.scalars().all()
    return [
        {
            "id": str(e.id),
            "team_id": e.team_id,
            "session_id": e.session_id,
            "seq_num": e.seq_num,
            "prev_hash": e.prev_hash,
            "entry_hash": e.entry_hash,
            "decision_hash": e.decision_hash,
            "proof_type": e.proof_type,
            "risk_level": e.risk_level,
            "tee_platform": e.tee_platform,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]


async def get_stats(db: AsyncSession, team_id: Optional[str] = None) -> dict:
    """Get ledger statistics."""
    from orchestrator.db.models import PoELedgerEntry, PoEMerkleAnchor

    q_entries = select(func.count(PoELedgerEntry.id))
    q_anchors = select(func.count(PoEMerkleAnchor.id))

    if team_id:
        q_entries = q_entries.where(PoELedgerEntry.team_id == team_id)
        q_anchors = q_anchors.where(PoEMerkleAnchor.team_id == team_id)

    entry_count = (await db.execute(q_entries)).scalar() or 0
    anchor_count = (await db.execute(q_anchors)).scalar() or 0

    # Proof type breakdown
    proof_types = {}
    for pt in ["hash_chain", "snark"]:
        q = select(func.count(PoELedgerEntry.id)).where(PoELedgerEntry.proof_type == pt)
        if team_id:
            q = q.where(PoELedgerEntry.team_id == team_id)
        proof_types[pt] = (await db.execute(q)).scalar() or 0

    return {
        "total_entries": entry_count,
        "total_anchors": anchor_count,
        "proof_types": proof_types,
        "sample_rate": settings.poe_sample_rate,
        "snark_enabled": settings.poe_snark_enabled,
        "high_risk_always_prove": settings.poe_high_risk_always_prove,
    }


async def run_poe_merkle_anchor_cycle() -> None:
    """Background task: build Merkle anchors for all teams with unanchored entries."""
    from orchestrator.db.session import get_session_ctx
    from orchestrator.db.models import PoELedgerEntry

    async with get_session_ctx() as db:
        # Get distinct teams with entries
        result = await db.execute(
            select(PoELedgerEntry.team_id).distinct()
        )
        team_ids = [r[0] for r in result.fetchall()]

        for team_id in team_ids:
            try:
                anchor = await build_merkle_anchor(db, team_id)
                if anchor:
                    logger.info(
                        "PoE Merkle anchor created",
                        extra={"team_id": team_id, "entries": anchor["entry_count"]},
                    )
            except Exception as exc:
                logger.error("PoE anchor failed for team %s: %s", team_id, exc)

        await db.commit()


# ── SLH-DSA Shim & Aggregate Proofs (Phase 10 B3) ────────────────────────────

_SLH_DSA_DOMAIN_TAG = b"modus-slh-dsa-poe-v1"


def sign_entry_slh_dsa(entry_hash: str, signer_key: bytes) -> dict:
    """
    SLH-DSA shim signature for an individual ledger entry.

    Uses HMAC-SHA-512 with domain separation as a post-quantum placeholder
    until real SLH-DSA libraries are stdlib-available.

    Parameters
    ----------
    entry_hash : str
        The SHA-256 hash of the ledger entry to sign. Must be non-empty.
    signer_key : bytes
        Signing key material. Must be >= 16 bytes.

    Returns
    -------
    dict
        ``{"signature": hex, "algorithm": "slh-dsa-shim-v1", "signed_at": ISO}``
    """
    if not entry_hash or not isinstance(entry_hash, str):
        raise ValueError("entry_hash must be a non-empty string")
    if not isinstance(signer_key, bytes) or len(signer_key) < 16:
        raise ValueError("signer_key must be bytes of length >= 16")

    message = _SLH_DSA_DOMAIN_TAG + b":" + entry_hash.encode()
    sig = hmac.new(signer_key, message, hashlib.sha512).hexdigest()

    return {
        "signature": sig,
        "algorithm": "slh-dsa-shim-v1",
        "signed_at": datetime.now(timezone.utc).isoformat(),
    }


def aggregate_proofs(entries: list[dict], signer_key: bytes) -> dict:
    """
    Compress multiple PoE entries into a single aggregate commitment.

    Combines entry hashes via Merkle tree and SHA-256 aggregation, then
    signs the aggregate with the SLH-DSA shim (HMAC — a symmetric MAC, not a
    signature). Optionally sets proof_type ``"aggregate_zk"`` via the
    EXPERIMENTAL ArithmeticCircuit when ``poe_snark_enabled`` is True; that
    is a label only, NOT a real zero-knowledge/SNARK proof.

    Parameters
    ----------
    entries : list[dict]
        List of PoE entry dicts, each containing at minimum ``seq_num``
        and ``entry_hash`` keys. Must be non-empty.
    signer_key : bytes
        Signing key material. Must be >= 16 bytes.

    Returns
    -------
    dict
        Aggregate proof containing hash, Merkle root, signature, and metadata.
    """
    if not entries or not isinstance(entries, list):
        raise ValueError("entries must be a non-empty list")
    if not isinstance(signer_key, bytes) or len(signer_key) < 16:
        raise ValueError("signer_key must be bytes of length >= 16")

    # Validate all entries have required keys
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entries[{i}] must be a dict")
        if "seq_num" not in entry or "entry_hash" not in entry:
            raise ValueError(f"entries[{i}] missing required keys: seq_num, entry_hash")

    # 1. Sort by seq_num for deterministic ordering
    sorted_entries = sorted(entries, key=lambda e: e["seq_num"])

    # 2. Collect hashes and compute aggregate
    entry_hashes = [e["entry_hash"] for e in sorted_entries]
    concatenated = "".join(entry_hashes)
    aggregate_hash = hashlib.sha256(concatenated.encode()).hexdigest()

    # 3. Build Merkle tree from entry hashes
    from orchestrator.core.merkle import MerkleTree
    tree = MerkleTree()
    for h in entry_hashes:
        tree.add_leaf(h)
    merkle_root = tree.build()

    # 4. Sign aggregate with SLH-DSA shim
    signature = sign_entry_slh_dsa(aggregate_hash, signer_key)

    # 5. Determine proof type — optionally attach SNARK
    proof_type = "aggregate_hash"
    if settings.poe_snark_enabled:
        try:
            from orchestrator.core.zk_circuit import ArithmeticCircuit
            circuit = ArithmeticCircuit()

            agg_val = int(aggregate_hash[:8], 16)
            root_val = int(merkle_root[:8], 16)

            circuit.allocate_public_input("aggregate_hash", agg_val)
            circuit.allocate_public_input("merkle_root", root_val)
            circuit.allocate_variable("entry_count", len(entry_hashes))

            if circuit.check_constraints():
                proof_type = "aggregate_zk"
        except Exception as exc:
            logger.debug("Aggregate Tier-2 circuit value failed, using aggregate_hash: %s", exc)

    return {
        "aggregate_hash": aggregate_hash,
        "merkle_root": merkle_root,
        "entry_count": len(entry_hashes),
        "first_seq": sorted_entries[0]["seq_num"],
        "last_seq": sorted_entries[-1]["seq_num"],
        "signature": signature,
        "proof_type": proof_type,
    }
