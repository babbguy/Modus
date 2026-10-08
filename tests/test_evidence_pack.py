"""
Regulatory evidence pack + compliance honesty + PDR.

Proof criteria:
  1. Evidence pack assembles framework-mapped governance decisions + audit
     integrity + policy proofs, signed and offline-verifiable.
  2. The standalone offline verifier PASSES a clean pack and REJECTS a pack
     whose content was altered after signing.
  3. Compliance report carries a LIVE chain-integrity probe (not an assertion).
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from orchestrator.core import audit_chain as ac
from orchestrator.core.evidence_pack import build_evidence_pack, pack_digest
from orchestrator.db.models import AuditLog, CoTLedgerEntry

_VERIFIER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_audit_export.py"
_spec = importlib.util.spec_from_file_location("verify_audit_export", _VERIFIER_PATH)
verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verifier)


@pytest.fixture(autouse=True)
def _chain_and_key(monkeypatch):
    monkeypatch.setenv("MODUS_ATTESTATION_KEY", "ef" * 32)
    installed = not event.contains(Session, "before_flush", ac._before_flush)
    if installed:
        event.listen(Session, "before_flush", ac._before_flush)
    yield
    if installed and event.contains(Session, "before_flush", ac._before_flush):
        event.remove(Session, "before_flush", ac._before_flush)


@pytest_asyncio.fixture
async def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _cot_entry(seq: int, prev_hash, tags) -> CoTLedgerEntry:
    return CoTLedgerEntry(
        id=str(uuid.uuid4()),
        team_id="a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
        seq_num=seq,
        prev_hash=prev_hash,
        entry_hash=f"cot_hash_{seq}",
        decision_type="governance_proposal",
        trigger="pattern_detection",
        decision_summary=f"decision {seq}",
        regulatory_tags=tags,
    )


async def _seed(db):
    # Audit + enforcement events (auto-chained)
    db.add(AuditLog(actor_id="admin", resource_type="policy", resource_id="p1",
                    action="created", after={"name": "cap"}))
    db.add(AuditLog(actor_id="enforcement-engine", resource_type="app",
                    resource_id="app-1", action="enforcement_deny",
                    after={"decision": "deny", "reason": "budget exceeded"}))
    # Governance decisions with framework tags
    db.add(_cot_entry(1, None, ["eu_ai_act_article_14", "nist_ai_rmf_govern_1"]))
    db.add(_cot_entry(2, "cot_hash_1", ["eu_ai_act_article_12"]))
    await db.commit()


# ── 1 + 2. Pack assembles, signs, verifies offline; tamper rejected ───────────

async def test_evidence_pack_offline_verify_roundtrip(factory, tmp_path):
    async with factory() as db:
        await _seed(db)
        pack = await build_evidence_pack(db)

    # Framework mapping present
    assert "eu_ai_act_article_14" in pack["frameworks"]
    assert pack["frameworks"]["eu_ai_act_article_12"]["decision_count"] == 1
    assert pack["summary"]["governance_decision_count"] == 2

    # Audit integrity carried and live-verified
    assert pack["enforcement_and_config_audit"]["chain_verified"] is True
    assert pack["enforcement_and_config_audit"]["enforcement_actions_in_scope"] == 1

    # Signed and offline-verifiable
    assert pack["signature"] is not None
    assert pack["public_key"] is not None

    digest_ok, _, sig_ok, sig_msg = verifier.verify_evidence_pack(pack)
    assert digest_ok is True
    assert sig_ok is True, sig_msg

    # Through the CLI entrypoint → exit 0
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    assert verifier.main(["verify_audit_export.py", str(path)]) == 0


async def test_evidence_pack_tamper_rejected(factory):
    async with factory() as db:
        await _seed(db)
        pack = await build_evidence_pack(db)

    # Alter a governance decision after signing.
    pack["governance_decisions"][0]["summary"] = "FORGED APPROVAL"

    digest_ok, digest_msg, sig_ok, _ = verifier.verify_evidence_pack(pack)
    assert digest_ok is False
    assert "altered" in digest_msg.lower()


async def test_evidence_pack_digest_excludes_envelope(factory):
    """The digest must be stable — recomputing over the body reproduces it."""
    async with factory() as db:
        await _seed(db)
        pack = await build_evidence_pack(db)

    body = {k: v for k, v in pack.items() if k not in verifier._PACK_ENVELOPE_KEYS}
    assert pack_digest(body) == pack["pack_digest"]


# ── 3. Compliance report live integrity probe ─────────────────────────────────

async def test_compliance_report_has_live_chain_probe(client):
    resp = await client.get("/api/v1/compliance/report")
    assert resp.status_code == 200
    audit = resp.json()["audit"]
    # Live, re-derivable signals rather than a bare assertion
    assert "chain_verified" in audit
    assert "chain_entries_verified" in audit
    assert "checkpoint_signing_available" in audit
