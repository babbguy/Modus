"""
Modus — Cryptographic Attestation Engine
===============================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Generates tamper-evident, HMAC-SHA256-signed attestations for every enforcement
decision. Batch operations anchor attestations into a Merkle tree so an entire
window of decisions can be verified against a single root hash.

Stdlib only. Zero dependencies. All signing operations target < 1ms.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from typing import Optional

from orchestrator.core.merkle import MerkleTree

logger = logging.getLogger(__name__)

_ALGORITHM = "hmac-sha256"


# ── HKDF (stdlib-only, RFC 5869) ─────────────────────────────────────────────

def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract: PRK = HMAC-Hash(salt, IKM)."""
    if not salt:
        salt = b"\x00" * 32  # hash length for SHA-256
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-Expand: derive *length* bytes of output keying material."""
    n = (length + 31) // 32  # ceil(L / HashLen)
    okm = b""
    t = b""
    for i in range(1, n + 1):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def _hkdf_derive(ikm: bytes, info: bytes = b"modus-attestation", length: int = 32) -> bytes:
    """Full HKDF: extract-then-expand with a fixed salt."""
    prk = _hkdf_extract(b"modus-hkdf-salt-v1", ikm)
    return _hkdf_expand(prk, info, length)


# ── Key resolution ────────────────────────────────────────────────────────────

def resolve_attestation_key() -> Optional[bytes]:
    """
    Resolve the attestation signing key from environment.

    Priority:
        1. MODUS_ATTESTATION_KEY — raw hex or base64 key (preferred)
        2. MODUS_ENCRYPTION_KEY — derived via HKDF into a separate key domain

    Returns None if no key material is available.
    """
    # Source 1: dedicated attestation key
    raw = os.environ.get("MODUS_ATTESTATION_KEY", "").strip()
    if raw:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            # Try UTF-8 bytes (e.g. a passphrase)
            return hashlib.sha256(raw.encode()).digest()

    # Source 2: derive from encryption key via HKDF
    enc_key = os.environ.get("MODUS_ENCRYPTION_KEY", "").strip()
    if enc_key:
        logger.debug("Deriving attestation key from MODUS_ENCRYPTION_KEY via HKDF")
        return _hkdf_derive(enc_key.encode())

    return None


# ── Attestation Signer ────────────────────────────────────────────────────────

class AttestationSigner:
    """
    Signs and verifies enforcement decision attestations using HMAC-SHA256.

    Thread-safe: the key is immutable after construction and HMAC operations
    are stateless.
    """

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if not key or len(key) < 16:
            raise ValueError("Attestation key must be at least 16 bytes")
        self._key = key

    # ── Signing ───────────────────────────────────────────────────────────────

    def sign_decision(
        self,
        decision_id: str,
        decision: str,
        policy_id: str,
        policy_type: str,
        app_id: str,
        team_id: str,
        timestamp: str,
    ) -> dict:
        """
        Generate an HMAC-SHA256 signed attestation for a single enforcement
        decision.

        Returns a dict containing all input fields plus *nonce*, *algorithm*,
        and *signature*.
        """
        nonce = secrets.token_hex(32)

        payload = {
            "decision_id": decision_id,
            "decision": decision,
            "policy_id": policy_id,
            "policy_type": policy_type,
            "app_id": app_id,
            "team_id": team_id,
            "timestamp": timestamp,
            "nonce": nonce,
            "algorithm": _ALGORITHM,
        }

        signature = self._compute_signature(payload)
        payload["signature"] = signature
        return payload

    def verify_attestation(self, payload: dict, signature: str) -> bool:
        """
        Verify an attestation signature using constant-time comparison.

        *payload* must contain all fields EXCEPT *signature* (or *signature*
        will be stripped before verification).
        """
        check_payload = {k: v for k, v in payload.items() if k != "signature"}
        expected = self._compute_signature(check_payload)
        return hmac.compare_digest(expected, signature)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _compute_signature(self, payload: dict) -> str:
        canonical = self._canonical_json(payload)
        return hmac.new(self._key, canonical, hashlib.sha256).hexdigest()

    @staticmethod
    def _canonical_json(payload: dict) -> bytes:
        """Deterministic JSON: sorted keys, no whitespace, UTF-8 encoded."""
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


# ── Batch signing with Merkle anchoring ───────────────────────────────────────

def batch_sign(
    signer: AttestationSigner,
    decisions: list[dict],
) -> tuple[list[dict], str]:
    """
    Sign a batch of enforcement decisions and anchor them into a Merkle tree.

    Each element in *decisions* must be a dict with the keys expected by
    ``AttestationSigner.sign_decision``:
        decision_id, decision, policy_id, policy_type, app_id, team_id, timestamp

    Returns:
        (attestations, merkle_root)

        *attestations* is a list of signed payloads, each augmented with a
        ``merkle_proof`` field containing the inclusion proof.

        *merkle_root* is the hex-encoded root hash of the Merkle tree.
    """
    if not decisions:
        raise ValueError("Cannot batch-sign an empty list")

    tree = MerkleTree()
    signed: list[dict] = []

    for d in decisions:
        attestation = signer.sign_decision(
            decision_id=d["decision_id"],
            decision=d["decision"],
            policy_id=d["policy_id"],
            policy_type=d["policy_type"],
            app_id=d["app_id"],
            team_id=d["team_id"],
            timestamp=d["timestamp"],
        )
        signed.append(attestation)

        # Leaf = SHA-256 of the canonical signed payload (including signature)
        leaf = hashlib.sha256(
            AttestationSigner._canonical_json(attestation)
        ).hexdigest()
        tree.add_leaf(leaf)

    merkle_root = tree.build()

    # Attach Merkle proofs to each attestation
    for i, att in enumerate(signed):
        att["merkle_proof"] = tree.get_proof(i)
        att["merkle_root"] = merkle_root

    return signed, merkle_root
