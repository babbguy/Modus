"""
Modus — Regulatory Evidence Pack
===================================
Assembles a single, signed, offline-verifiable evidence bundle from the
cryptographically-sound sources built in Items 1-2:

  - the tamper-evident audit chain (config changes + enforcement actions),
  - the Chain-of-Thought governance ledger (autonomous decisions, already
    tagged to EU AI Act / NIST AI RMF / ISO 42001 articles),
  - policy formal-verification certificates (PolicyProof).

Governance decisions are grouped by the regulatory framework they map to, so
the pack answers an examiner's question directly ("show me every AI decision
relevant to EU AI Act Article 14"). The whole pack is hashed and the digest is
Ed25519-signed with the deployment's checkpoint key, so the pack — like an
audit export — verifies offline with the public key alone.

Best-effort by design: a source that is empty or disabled simply contributes
nothing; the pack never fabricates content.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

PACK_FORMAT = "modus-evidence-pack-v1"

# Human-readable framework labels for the tags the CoT ledger emits.
FRAMEWORK_LABELS = {
    "eu_ai_act_article_9": "EU AI Act Art. 9 — Risk management",
    "eu_ai_act_article_10": "EU AI Act Art. 10 — Data governance",
    "eu_ai_act_article_12": "EU AI Act Art. 12 — Record-keeping / logging",
    "eu_ai_act_article_14": "EU AI Act Art. 14 — Human oversight",
    "eu_ai_act_article_15": "EU AI Act Art. 15 — Accuracy & robustness",
    "eu_ai_act_article_17": "EU AI Act Art. 17 — Quality management",
    "nist_ai_rmf_govern_1": "NIST AI RMF — GOVERN 1",
    "nist_ai_rmf_measure_2": "NIST AI RMF — MEASURE 2",
    "nist_ai_rmf_manage_2": "NIST AI RMF — MANAGE 2",
    "nist_pqc": "NIST Post-Quantum Cryptography",
    "iso_42001": "ISO/IEC 42001 — AI management system",
    "interagency_model_risk": "US Interagency Model-Risk Guidance (SR 11-7 / 2026 GenAI)",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def pack_digest(pack_body: dict) -> str:
    """SHA-256 over the canonical pack body (everything except the signature)."""
    return hashlib.sha256(_canonical(pack_body).encode("utf-8")).hexdigest()


async def build_evidence_pack(
    db,
    *,
    team_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> dict:
    """Assemble a signed evidence pack over the given window (all-time if unset)."""
    from orchestrator.db.models import AuditLog, CoTLedgerEntry, PolicyProof
    from orchestrator.core.audit_chain import canonical_timestamp, verify_audit_chain
    from orchestrator.core.audit_checkpoint import (
        public_key_hex, checkpoint_signing_available, sign_checkpoint,
    )

    generated_at = datetime.now(timezone.utc)

    # ── 1. Governance decisions (CoT ledger), grouped by framework ────────────
    cot_q = select(CoTLedgerEntry).order_by(CoTLedgerEntry.seq_num.asc())
    if team_id:
        cot_q = cot_q.where(CoTLedgerEntry.team_id == team_id)
    if since:
        cot_q = cot_q.where(CoTLedgerEntry.created_at >= since)
    if until:
        cot_q = cot_q.where(CoTLedgerEntry.created_at <= until)
    cot_rows = (await db.execute(cot_q)).scalars().all()

    decisions = []
    frameworks: dict[str, dict] = {}
    for r in cot_rows:
        tags = list(r.regulatory_tags or [])
        decision = {
            "seq_num": r.seq_num,
            "entry_hash": r.entry_hash,
            "decision_type": r.decision_type,
            "trigger": r.trigger,
            "summary": r.decision_summary,
            "regulatory_tags": tags,
            "created_at": canonical_timestamp(r.created_at),
        }
        decisions.append(decision)
        for tag in tags:
            fw = frameworks.setdefault(tag, {
                "label": FRAMEWORK_LABELS.get(tag, tag),
                "decision_count": 0,
                "decision_seqs": [],
            })
            fw["decision_count"] += 1
            fw["decision_seqs"].append(r.seq_num)

    # ── 2. Enforcement + config audit trail (tamper-evident) ──────────────────
    audit_q = (
        select(AuditLog)
        .where(AuditLog.chain_seq.isnot(None))
        .order_by(AuditLog.chain_seq.asc())
    )
    if team_id:
        audit_q = audit_q.where(AuditLog.team_id == team_id)
    if since:
        audit_q = audit_q.where(AuditLog.occurred_at >= since)
    if until:
        audit_q = audit_q.where(AuditLog.occurred_at <= until)
    audit_rows = (await db.execute(audit_q)).scalars().all()
    audit_head = audit_rows[-1] if audit_rows else None

    chain_result = await verify_audit_chain(db)
    enforcement_actions = sum(
        1 for r in audit_rows if r.action.startswith("enforcement_")
    )

    # ── 3. Policy formal-verification certificates ────────────────────────────
    proof_rows = (await db.execute(
        select(PolicyProof).order_by(PolicyProof.proven_at.desc()).limit(500)
    )).scalars().all()
    proofs = [
        {
            "policy_id": p.policy_id,
            "proof_type": p.proof_type,
            "status": p.proof_status,
            "proven_at": canonical_timestamp(p.proven_at) if p.proven_at else None,
        }
        for p in proof_rows
    ]

    # ── Assemble the signable body ────────────────────────────────────────────
    pack_body = {
        "format": PACK_FORMAT,
        "generated_at": canonical_timestamp(generated_at),
        "scope": {
            "team_id": team_id,
            "since": canonical_timestamp(since) if since else None,
            "until": canonical_timestamp(until) if until else None,
        },
        "frameworks": frameworks,
        "governance_decisions": decisions,
        "enforcement_and_config_audit": {
            "chain_verified": chain_result.valid,
            "chain_entries_verified": chain_result.entries_checked,
            "chain_break_at_seq": chain_result.break_seq,
            "audit_entries_in_scope": len(audit_rows),
            "enforcement_actions_in_scope": enforcement_actions,
            "head_chain_seq": audit_head.chain_seq if audit_head else None,
            "head_entry_hash": audit_head.entry_hash if audit_head else None,
            "full_trail_export": "GET /api/v1/admin/audit-log/export",
        },
        "policy_proofs": proofs,
        "summary": {
            "governance_decision_count": len(decisions),
            "frameworks_covered": sorted(frameworks.keys()),
            "policy_proof_count": len(proofs),
        },
    }

    # ── Sign the pack digest (offline-verifiable, public-key-only) ────────────
    digest = pack_digest(pack_body)
    signature = None
    signed_at_iso = canonical_timestamp(generated_at)
    if checkpoint_signing_available():
        # Reuse the checkpoint signing scheme: sign a message binding the digest.
        signature = sign_checkpoint(0, digest, signed_at_iso)

    return {
        **pack_body,
        "public_key": public_key_hex(),
        "algorithm": "ed25519",
        "pack_digest": digest,
        "signature": signature,
        "signed_at": signed_at_iso,
        "verification": (
            "Verify offline with: python scripts/verify_audit_export.py <pack.json>. "
            "It recomputes the pack digest over the canonical body and checks the "
            "Ed25519 signature against the embedded public key. The full audit trail "
            "referenced here is separately exportable and offline-verifiable."
        ),
    }
