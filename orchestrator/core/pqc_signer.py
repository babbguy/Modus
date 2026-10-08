"""
Modus — Attestation Signer (HMAC default, optional PQC add-on)
==================================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Signs enforcement attestations. Two tiers with very different guarantees:

Tier 1 (DEFAULT, stdlib only): HMAC-SHA-512 hash-chain commitment with a
512-bit derived per-epoch key and domain separation. This is a symmetric MAC,
NOT a digital signature — it provides quantum-resistant integrity/tamper-
evidence, but it is NOT publicly verifiable: only a holder of the shared
attestation key can verify it. Zero dependencies.

Tier 2 (OPTIONAL add-on, requires the ``pqcrypto`` package): real ML-DSA-65
(Dilithium3) post-quantum digital signatures — publicly verifiable with the
public key. Only available when ``pqcrypto`` is installed; there is no silent
downgrade (a ``real`` tier without pqcrypto reports as unavailable).

``resolve_algorithm`` reports the algorithm actually in force at runtime, so
status never advertises ML-DSA when only the HMAC shim is active.

Thread-safe: all state is immutable after __init__.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from orchestrator.core.attestation_engine import _hkdf_derive

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_DOMAIN_TAG = b"modus-pqc-shim-v1"
_PQC_KEY_INFO = b"modus-pqc-signing-key"
_PQC_EPOCH_DEFAULT = 86400  # 24 hours in seconds
_PQC_KEY_LENGTH = 64  # 512 bytes = 4096 bits (derived key for HMAC-SHA-512)

_ALGO_SHIM = "pqc-shim-hmac-sha512"
_ALGO_DILITHIUM = "ml-dsa-65"

# ── Tier 2: Optional pqcrypto ────────────────────────────────────────────────

_HAS_PQCRYPTO = False
_dilithium_sign = None
_dilithium_verify = None
_dilithium_keypair = None

try:
    # FIPS 204 standardized name is ml_dsa_65; older pqcrypto shipped it as
    # dilithium3 (the Round-3 name for the same parameter set). Accept both.
    try:
        from pqcrypto.sign.ml_dsa_65 import sign, verify, generate_keypair  # type: ignore[import-untyped]
    except ImportError:
        from pqcrypto.sign.dilithium3 import sign, verify, generate_keypair  # type: ignore[import-untyped]

    _dilithium_sign = sign
    _dilithium_verify = verify
    _dilithium_keypair = generate_keypair
    _HAS_PQCRYPTO = True
    logger.info("pqcrypto available — ML-DSA-65 enabled")
except ImportError:
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_tier(tier: str) -> str:
    """Map the config vocabulary (auto|simulated|real) onto signer tiers.

    The settings field ``pqc_attestation_tier`` speaks ``simulated``/``real``
    while the signer historically spoke ``tier1``/``tier2``; both are accepted
    everywhere so a configured tier is never silently ignored.
    """
    t = tier.lower().strip()
    return {"simulated": "tier1", "real": "tier2"}.get(t, t)


def resolve_algorithm(tier: str) -> str:
    """Return the algorithm a signer built with this tier would ACTUALLY use.

    Status reporting must reflect runtime reality, never the config
    aspiration: ``real`` without pqcrypto installed is unavailable, not
    silently downgraded while advertising ML-DSA.
    """
    t = normalize_tier(tier)
    if t == "tier2":
        return _ALGO_DILITHIUM if _HAS_PQCRYPTO else "unavailable:pqcrypto-not-installed"
    if t == "auto":
        return _ALGO_DILITHIUM if _HAS_PQCRYPTO else _ALGO_SHIM
    return _ALGO_SHIM


def _epoch_id(epoch_seconds: int) -> int:
    """Return the current epoch number (monotonically increasing)."""
    return int(time.time()) // epoch_seconds


def _derive_epoch_key(master_key: bytes, epoch: int) -> bytes:
    """
    Derive a per-epoch signing key via HKDF.

    Each epoch gets a unique key so that compromise of one epoch's key does
    not affect attestations from other epochs.
    """
    epoch_info = _PQC_KEY_INFO + b":" + str(epoch).encode()
    return _hkdf_derive(master_key, info=epoch_info, length=_PQC_KEY_LENGTH)


def _canonical_json(payload: str) -> bytes:
    """
    Produce canonical bytes from a JSON string for signing.

    Re-parses and re-serializes to guarantee deterministic key ordering
    and separator formatting, then prepends the domain separation tag.
    """
    parsed = json.loads(payload)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()
    return _DOMAIN_TAG + b":" + canonical


# ── PQCAttestationSigner ─────────────────────────────────────────────────────

class PQCAttestationSigner:
    """
    Attestation signer. Default tier is an HMAC-SHA-512 hash-chain commitment
    (a symmetric MAC — quantum-resistant integrity, not a publicly-verifiable
    signature). The optional Tier 2 uses real ML-DSA-65 when pqcrypto is
    installed.

    Thread-safe: the master key, keypair, and epoch configuration are
    immutable after construction. Epoch key derivation is deterministic
    and uses no shared mutable state.

    Parameters
    ----------
    attestation_key : str
        Raw key material (hex string or passphrase). Used as IKM for
        HKDF to derive per-epoch signing keys.
    tier : str
        ``"auto"`` (default) — use Tier 2 if pqcrypto is installed, else Tier 1.
        ``"tier1"`` — force Tier 1 (HMAC-SHA-512 hash-chain commitment; a
        symmetric MAC, not a publicly-verifiable signature).
        ``"tier2"`` — force Tier 2 (real ML-DSA-65). Raises if pqcrypto missing.
    epoch_seconds : int
        Key rotation epoch length in seconds. Default 86400 (24 hours).
    """

    __slots__ = (
        "_master_key",
        "_algorithm",
        "_epoch_seconds",
        "_dilithium_pk",
        "_dilithium_sk",
        "_public_key_id",
    )

    def __init__(
        self,
        attestation_key: str,
        tier: str = "auto",
        epoch_seconds: int = _PQC_EPOCH_DEFAULT,
    ) -> None:
        if not attestation_key or not attestation_key.strip():
            raise ValueError("attestation_key must be a non-empty string")

        # Derive master key from input material
        raw = attestation_key.strip()
        try:
            ikm = bytes.fromhex(raw)
        except ValueError:
            ikm = hashlib.sha256(raw.encode()).digest()

        if len(ikm) < 16:
            raise ValueError("attestation_key must yield at least 16 bytes of key material")

        self._master_key: bytes = ikm
        self._epoch_seconds: int = max(1, epoch_seconds)

        # Dilithium keypair (Tier 2 only)
        self._dilithium_pk: Optional[bytes] = None
        self._dilithium_sk: Optional[bytes] = None

        # Resolve tier (accepts both config vocabulary and tier1/tier2)
        resolved_tier = normalize_tier(tier)
        if resolved_tier == "auto":
            resolved_tier = "tier2" if _HAS_PQCRYPTO else "tier1"

        if resolved_tier == "tier2":
            if not _HAS_PQCRYPTO:
                raise RuntimeError(
                    "Tier 2 (ML-DSA-65) requested but pqcrypto is not installed. "
                    "Install with: pip install pqcrypto"
                )
            assert _dilithium_keypair is not None
            self._dilithium_pk, self._dilithium_sk = _dilithium_keypair()
            self._algorithm: str = _ALGO_DILITHIUM
            logger.info("PQC signer initialised: ML-DSA-65 (Dilithium3)")
        else:
            self._algorithm = _ALGO_SHIM
            logger.info(
                "PQC signer initialised: HMAC-SHA-512 hash-chain commitment "
                "(symmetric MAC, not a publicly-verifiable signature)"
            )

        # Public key ID — stable identifier for verification
        if self._dilithium_pk is not None:
            self._public_key_id: str = hashlib.sha256(self._dilithium_pk).hexdigest()[:32]
        else:
            # For Tier 1, derive a stable ID from the master key
            self._public_key_id = hashlib.sha256(
                _PQC_KEY_INFO + self._master_key
            ).hexdigest()[:32]

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def algorithm(self) -> str:
        """The signature algorithm this signer actually uses at runtime."""
        return self._algorithm

    def sign(self, payload_json: str) -> dict:
        """
        Sign a canonical JSON payload.

        Returns a dict with:
            - ``pqc_signature``: hex-encoded signature string
            - ``pqc_algorithm``: algorithm identifier
            - ``pqc_public_key_id``: stable key identifier for verification
        """
        if not payload_json:
            raise ValueError("payload_json must be non-empty")

        if self._algorithm == _ALGO_DILITHIUM:
            signature = self._sign_dilithium(payload_json)
        else:
            signature = self._sign_shim(payload_json)

        return {
            "pqc_signature": signature,
            "pqc_algorithm": self._algorithm,
            "pqc_public_key_id": self._public_key_id,
        }

    def verify(self, payload_json: str, signature: str, algorithm: str) -> bool:
        """
        Verify a PQC signature against a payload.

        Parameters
        ----------
        payload_json : str
            The canonical JSON payload that was signed.
        signature : str
            The hex-encoded signature to verify.
        algorithm : str
            The algorithm used to produce the signature.

        Returns
        -------
        bool
            True if the signature is valid, False otherwise.
        """
        if not payload_json or not signature or not algorithm:
            return False

        try:
            if algorithm == _ALGO_DILITHIUM:
                return self._verify_dilithium(payload_json, signature)
            elif algorithm == _ALGO_SHIM:
                return self._verify_shim(payload_json, signature)
            else:
                logger.warning("Unknown PQC algorithm for verification: %s", algorithm)
                return False
        except Exception:
            logger.exception("PQC verification failed")
            return False

    def get_public_key_id(self) -> str:
        """Return the stable public key identifier for this signer."""
        return self._public_key_id

    # ── Tier 1: HMAC-SHA-512 hash-chain commitment (symmetric MAC) ───────────

    def _sign_shim(self, payload_json: str) -> str:
        """
        Hash-chain commitment scheme using HMAC-SHA-512.

        1. Derive per-epoch key via HKDF from master key.
        2. Prepend domain separation tag to canonical payload.
        3. Compute HMAC-SHA-512 — output is 128-char hex (512 bits).
        """
        epoch = _epoch_id(self._epoch_seconds)
        epoch_key = _derive_epoch_key(self._master_key, epoch)

        message = _canonical_json(payload_json)
        sig = hmac.new(epoch_key, message, hashlib.sha512).hexdigest()
        return sig  # 128 hex chars

    def _verify_shim(self, payload_json: str, signature: str) -> bool:
        """
        Verify a Tier 1 shim signature.

        Checks the current epoch and the previous epoch to handle clock
        boundaries gracefully.
        """
        message = _canonical_json(payload_json)
        current_epoch = _epoch_id(self._epoch_seconds)

        # Check current and previous epoch to handle boundary transitions
        for epoch in (current_epoch, current_epoch - 1):
            epoch_key = _derive_epoch_key(self._master_key, epoch)
            expected = hmac.new(epoch_key, message, hashlib.sha512).hexdigest()
            if hmac.compare_digest(expected, signature):
                return True

        return False

    # ── Tier 2: ML-DSA-65 (Dilithium3) ───────────────────────────────────────

    def _sign_dilithium(self, payload_json: str) -> str:
        """Sign with ML-DSA-65 and return hex-encoded signature."""
        assert _dilithium_sign is not None
        assert self._dilithium_sk is not None

        message = _canonical_json(payload_json)
        raw_sig = _dilithium_sign(self._dilithium_sk, message)
        return raw_sig.hex()

    def _verify_dilithium(self, payload_json: str, signature: str) -> bool:
        """Verify an ML-DSA-65 signature.

        pqcrypto has two verify conventions across versions: some return a
        bool (False on a bad signature), others return None on success and
        raise on failure. Honor BOTH — the previous code ignored the bool and
        unconditionally returned True, so it accepted any signature.
        """
        assert _dilithium_verify is not None
        assert self._dilithium_pk is not None

        message = _canonical_json(payload_json)
        try:
            raw_sig = bytes.fromhex(signature)
        except ValueError:
            return False
        try:
            result = _dilithium_verify(self._dilithium_pk, message, raw_sig)
        except Exception:
            return False  # raised → invalid
        if result is None:
            return True    # didn't raise, no bool → valid (raise-convention build)
        return bool(result)

    # ── repr ──────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<PQCAttestationSigner algorithm={self._algorithm} "
            f"key_id={self._public_key_id[:8]}...>"
        )


# ── SLH-DSA Shim & Agent Key Derivation (Phase 10 B3) ────────────────────────

_SLH_DSA_SHIM_DOMAIN = b"modus-slh-dsa-shim-v1"
_AGENT_KEY_INFO_PREFIX = b"modus-agent-key-v1:"


def _sign_slh_dsa_shim(payload: bytes, key: bytes) -> str:
    """
    HMAC-SHA-512 commitment with SLH-DSA domain separation.

    Provides a stdlib-only placeholder for SLH-DSA. This is a symmetric MAC
    (quantum-resistant integrity), NOT a digital signature — it is not
    publicly verifiable. The domain separation tag ensures these outputs are
    cryptographically distinct from other HMAC usages in the system.

    Parameters
    ----------
    payload : bytes
        The message bytes to sign. Must be non-empty.
    key : bytes
        Signing key material. Must be non-empty bytes.

    Returns
    -------
    str
        Hex-encoded HMAC-SHA-512 signature (128 characters).
    """
    if not isinstance(payload, bytes) or len(payload) == 0:
        raise ValueError("payload must be non-empty bytes")
    if not isinstance(key, bytes) or len(key) == 0:
        raise ValueError("key must be non-empty bytes")

    message = _SLH_DSA_SHIM_DOMAIN + b":" + payload
    return hmac.new(key, message, hashlib.sha512).hexdigest()


def derive_agent_key(
    platform_root_key: bytes,
    agent_fingerprint: str,
    key_length: int = 32,
) -> bytes:
    """
    Derive an agent-specific signing key from the platform root key via HKDF.

    Each agent gets a unique deterministic key so that compromise of one
    agent's key does not affect other agents on the platform.

    Parameters
    ----------
    platform_root_key : bytes
        Platform-wide root key material. Must be >= 16 bytes.
    agent_fingerprint : str
        Unique identifier for the agent (e.g. hostname hash, SDK fingerprint).
        Must be non-empty.
    key_length : int
        Desired output key length in bytes. Default 32 (256 bits).
        Must be between 16 and 128 inclusive.

    Returns
    -------
    bytes
        Derived key of ``key_length`` bytes.
    """
    if not isinstance(platform_root_key, bytes) or len(platform_root_key) < 16:
        raise ValueError("platform_root_key must be bytes of length >= 16")
    if not agent_fingerprint or not isinstance(agent_fingerprint, str):
        raise ValueError("agent_fingerprint must be a non-empty string")
    if not isinstance(key_length, int) or key_length < 16 or key_length > 128:
        raise ValueError("key_length must be an integer between 16 and 128")

    info = _AGENT_KEY_INFO_PREFIX + agent_fingerprint.encode()
    return _hkdf_derive(platform_root_key, info=info, length=key_length)
