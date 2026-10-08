"""
Modus — Audit Chain Checkpoints (signed, externally verifiable)
==================================================================
Item 1 makes the audit chain tamper-evident *to the operator*. This module
makes it verifiable *by a third party* — an examiner who does not trust the
operator and holds only a public key.

A checkpoint binds a point in the chain (``chain_seq`` + that entry's
``entry_hash``) and is signed with an Ed25519 private key derived from the
deployment's attestation key. The matching public key is published (via the
export manifest and a status endpoint). Because the signature is asymmetric,
the examiner can verify a checkpoint with the public key alone and *cannot*
forge one — unlike the HMAC primitives elsewhere, which the verifier could
also use to sign.

Signing here uses ``cryptography`` (already a pinned dependency). Verification
is intentionally reimplemented in pure stdlib in ``scripts/verify_audit_export``
so an examiner can run it on an air-gapped machine with only Python.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_CHECKPOINT_KEY_INFO = b"modus-audit-checkpoint-ed25519-v1"


def _derive_seed() -> Optional[bytes]:
    """Derive a stable 32-byte Ed25519 seed from the attestation key.

    Returns None if no key material is configured — checkpoints are then
    unavailable (the chain is still tamper-evident, just not externally
    signable) and endpoints report that honestly.
    """
    from orchestrator.core.attestation_engine import resolve_attestation_key, _hkdf_derive

    key = resolve_attestation_key()
    if not key:
        return None
    # Separate key domain so the checkpoint key can't be confused with the
    # attestation/HMAC key even though both derive from the same root.
    return _hkdf_derive(key, info=_CHECKPOINT_KEY_INFO, length=32)


def checkpoint_signing_available() -> bool:
    return _derive_seed() is not None


def _private_key():
    seed = _derive_seed()
    if seed is None:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_hex() -> Optional[str]:
    """Return the checkpoint public key as hex, or None if unconfigured."""
    from cryptography.hazmat.primitives import serialization

    priv = _private_key()
    if priv is None:
        return None
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def checkpoint_message(chain_seq: int, entry_hash: str, created_at_iso: str) -> bytes:
    """Canonical bytes that a checkpoint signs. Must match the verifier."""
    return f"modus-audit-checkpoint-v1|{chain_seq}|{entry_hash}|{created_at_iso}".encode("utf-8")


def sign_checkpoint(chain_seq: int, entry_hash: str, created_at_iso: str) -> Optional[str]:
    """Sign a checkpoint. Returns hex signature, or None if unconfigured."""
    priv = _private_key()
    if priv is None:
        return None
    msg = checkpoint_message(chain_seq, entry_hash, created_at_iso)
    return priv.sign(msg).hex()


async def create_checkpoint(db) -> Optional[dict]:
    """Sign the current chain head and persist an AuditCheckpoint row.

    Returns the checkpoint dict, or None if the chain is empty or signing is
    unconfigured. Idempotent-ish: a checkpoint at an already-checkpointed head
    is skipped to avoid unbounded duplicate rows.
    """
    from sqlalchemy import select, desc
    from orchestrator.db.models import AuditLog, AuditCheckpoint

    if not checkpoint_signing_available():
        return None

    head = (await db.execute(
        select(AuditLog)
        .where(AuditLog.chain_seq.isnot(None))
        .order_by(desc(AuditLog.chain_seq))
        .limit(1)
    )).scalar_one_or_none()
    if head is None:
        return None

    last_cp = (await db.execute(
        select(AuditCheckpoint).order_by(desc(AuditCheckpoint.chain_seq)).limit(1)
    )).scalar_one_or_none()
    if last_cp is not None and last_cp.chain_seq == head.chain_seq:
        return _cp_dict(last_cp)

    created_at = datetime.now(timezone.utc)
    created_iso = created_at.replace(tzinfo=None).isoformat()
    signature = sign_checkpoint(head.chain_seq, head.entry_hash, created_iso)

    cp = AuditCheckpoint(
        chain_seq=head.chain_seq,
        entry_hash=head.entry_hash,
        signature=signature,
        public_key_id=hashlib.sha256((public_key_hex() or "").encode()).hexdigest()[:32],
        created_at=created_at,
    )
    db.add(cp)
    await db.flush()
    logger.info("Audit checkpoint signed at seq %d", head.chain_seq)
    return _cp_dict(cp)


def _cp_dict(cp) -> dict:
    return {
        "chain_seq": cp.chain_seq,
        "entry_hash": cp.entry_hash,
        "signature": cp.signature,
        "public_key_id": cp.public_key_id,
        "created_at": cp.created_at.replace(tzinfo=None).isoformat()
        if cp.created_at.tzinfo else cp.created_at.isoformat(),
    }
