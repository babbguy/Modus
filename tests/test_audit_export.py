"""
Signed exports + offline verifier.

Proof criteria:
  1. Export → offline verify round-trip PASSES on a clean export.
  2. Tampering with an exported entry is REJECTED by the offline verifier.
  3. A forged/blank checkpoint signature is REJECTED.
  4. The checkpoint signature verifies with the PUBLIC KEY ALONE (no server,
     no private key) — via the pure-stdlib verifier.
  5. The offline verifier is genuinely dependency-free (pure stdlib).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from orchestrator.core import audit_chain as ac
from orchestrator.core import audit_checkpoint as cp
from orchestrator.core.audit_chain import canonical_timestamp
from orchestrator.db.models import AuditLog

# Load the standalone verifier by path — it must import with stdlib only.
_VERIFIER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_audit_export.py"
_spec = importlib.util.spec_from_file_location("verify_audit_export", _VERIFIER_PATH)
verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verifier)


@pytest.fixture(autouse=True)
def _chain_and_key(monkeypatch):
    """Install the chain listener and configure a deterministic signing key."""
    monkeypatch.setenv("MODUS_ATTESTATION_KEY", "00" * 32)
    installed = not event.contains(Session, "before_flush", ac._before_flush)
    if installed:
        event.listen(Session, "before_flush", ac._before_flush)
    yield
    if installed and event.contains(Session, "before_flush", ac._before_flush):
        event.remove(Session, "before_flush", ac._before_flush)


@pytest_asyncio.fixture
async def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _audit(action: str) -> AuditLog:
    return AuditLog(
        actor_id="tester", resource_type="app", resource_id="app-1",
        action=action, after={"k": action},
    )


async def _build_export(db) -> dict:
    """Reproduce what GET /audit-log/export returns, without the HTTP layer."""
    from sqlalchemy import select
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.chain_seq.isnot(None))
        .order_by(AuditLog.chain_seq.asc())
    )).scalars().all()
    chain = [{
        "chain_seq": r.chain_seq, "prev_hash": r.prev_hash, "entry_hash": r.entry_hash,
        "actor_id": r.actor_id, "actor_ip": r.actor_ip, "team_id": r.team_id,
        "resource_type": r.resource_type, "resource_id": r.resource_id,
        "action": r.action, "before": r.before, "after": r.after,
        "occurred_at": canonical_timestamp(r.occurred_at),
    } for r in rows]
    checkpoint = await cp.create_checkpoint(db)
    return {
        "format": "modus-audit-export-v1",
        "public_key": cp.public_key_hex(),
        "algorithm": "ed25519",
        "entry_count": len(chain),
        "chain": chain,
        "checkpoint": checkpoint,
    }


# ── 1. Round-trip PASS ────────────────────────────────────────────────────────

async def test_export_verifies_offline(factory, tmp_path):
    async with factory() as db:
        for i in range(8):
            db.add(_audit(f"a{i}"))
        await db.commit()
        export = await _build_export(db)

    assert export["checkpoint"] is not None
    assert export["public_key"] is not None

    chain_ok, _ = verifier.verify_chain(export["chain"])
    cp_ok, cp_msg = verifier.verify_checkpoint(export)
    assert chain_ok is True
    assert cp_ok is True, cp_msg

    # And through the CLI entrypoint against a written file → exit 0
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    assert verifier.main(["verify_audit_export.py", str(path)]) == 0


# ── 2. Tamper rejected ────────────────────────────────────────────────────────

async def test_offline_verifier_rejects_tampered_entry(factory):
    async with factory() as db:
        for i in range(5):
            db.add(_audit(f"t{i}"))
        await db.commit()
        export = await _build_export(db)

    # Tamper with a middle entry's content (as a malicious operator would in
    # the exported evidence).
    export["chain"][2]["after"] = {"k": "FORGED"}

    chain_ok, msg = verifier.verify_chain(export["chain"])
    assert chain_ok is False
    assert "tampered" in msg.lower()


# ── 3. Forged checkpoint signature rejected ───────────────────────────────────

async def test_offline_verifier_rejects_forged_signature(factory):
    async with factory() as db:
        db.add(_audit("x"))
        await db.commit()
        export = await _build_export(db)

    export["checkpoint"]["signature"] = "00" * 64  # blank/forged
    cp_ok, msg = verifier.verify_checkpoint(export)
    assert cp_ok is False
    assert "invalid" in msg.lower() or "unsigned" in msg.lower()


# ── 4. Signature verifies with the public key alone ───────────────────────────

async def test_checkpoint_verifies_with_public_key_only(factory):
    async with factory() as db:
        for i in range(3):
            db.add(_audit(f"p{i}"))
        await db.commit()
        export = await _build_export(db)

    # Reconstruct verification using ONLY the public key + verifier (no server
    # objects, no private key material).
    c = export["checkpoint"]
    msg = verifier.checkpoint_message(c["chain_seq"], c["entry_hash"], c["created_at"])
    ok = verifier.ed25519_verify(
        bytes.fromhex(export["public_key"]), msg, bytes.fromhex(c["signature"])
    )
    assert ok is True

    # A different key must NOT verify it.
    other_pub = bytes(range(32))
    assert verifier.ed25519_verify(other_pub, msg, bytes.fromhex(c["signature"])) is False


# ── 5. Verifier is dependency-free ────────────────────────────────────────────

def test_verifier_is_stdlib_only():
    """The offline verifier must import only stdlib modules — an examiner runs
    it on an air-gapped box with a bare Python."""
    source = _VERIFIER_PATH.read_text(encoding="utf-8")
    third_party = ("cryptography", "sqlalchemy", "fastapi", "pydantic",
                   "orchestrator", "nacl", "ecdsa", "numpy")
    for name in third_party:
        assert f"import {name}" not in source and f"from {name}" not in source, (
            f"offline verifier must not import {name}"
        )
