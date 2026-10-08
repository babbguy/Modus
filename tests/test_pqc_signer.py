"""
Tests for orchestrator.core.pqc_signer — Post-Quantum Sovereign Attestation
Signer with hybrid classical/PQC output, epoch-based key rotation, and
sign/verify round-trips.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.core.pqc_signer import (
    PQCAttestationSigner,
    _derive_epoch_key,
    _ALGO_SHIM,
    _HAS_PQCRYPTO,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _signer(key: str = "test-key-for-pqc-signer-unit-tests") -> PQCAttestationSigner:
    return PQCAttestationSigner(key, tier="tier1")


def _payload() -> str:
    return json.dumps({
        "decision_id": "dec-pqc-001",
        "decision": "allow",
        "policy_id": "pol-1",
        "app_id": "app1",
        "timestamp": "2026-03-14T00:00:00Z",
    })


# ── PQCAttestationSigner: Sign / Verify ─────────────────────────────────────


class TestPQCSignVerify:
    """Round-trip sign and verify for the Tier 1 (HMAC-SHA-512 shim) signer."""

    def test_sign_returns_required_keys(self):
        """sign() result must contain pqc_signature, pqc_algorithm, pqc_public_key_id."""
        signer = _signer()
        result = signer.sign(_payload())
        assert "pqc_signature" in result
        assert "pqc_algorithm" in result
        assert "pqc_public_key_id" in result

    def test_sign_verify_round_trip(self):
        """A signature produced by sign() must verify with the same signer."""
        signer = _signer()
        payload = _payload()
        result = signer.sign(payload)
        assert signer.verify(
            payload, result["pqc_signature"], result["pqc_algorithm"]
        )

    def test_tamper_detection_modified_payload(self):
        """Modifying the payload after signing must fail verification."""
        signer = _signer()
        payload = _payload()
        result = signer.sign(payload)
        tampered = json.dumps({"decision_id": "TAMPERED", "decision": "deny"})
        assert signer.verify(
            tampered, result["pqc_signature"], result["pqc_algorithm"]
        ) is False

    def test_tamper_detection_modified_signature(self):
        """A corrupted signature must fail verification."""
        signer = _signer()
        payload = _payload()
        result = signer.sign(payload)
        bad_sig = "0" * len(result["pqc_signature"])
        assert signer.verify(payload, bad_sig, result["pqc_algorithm"]) is False

    def test_verify_empty_inputs_returns_false(self):
        """verify() with empty payload, signature, or algorithm returns False."""
        signer = _signer()
        assert signer.verify("", "abc123", _ALGO_SHIM) is False
        assert signer.verify(_payload(), "", _ALGO_SHIM) is False
        assert signer.verify(_payload(), "abc123", "") is False

    def test_sign_empty_payload_raises(self):
        """sign() must raise ValueError on empty payload."""
        signer = _signer()
        with pytest.raises(ValueError, match="non-empty"):
            signer.sign("")


# ── PQCAttestationSigner: Tier Detection ─────────────────────────────────────


class TestPQCTierDetection:
    """Tier resolution and algorithm attribute checks."""

    def test_tier1_algorithm_is_shim(self):
        """Tier 1 signer reports pqc-shim-hmac-sha512 algorithm."""
        signer = _signer()
        assert signer.algorithm == _ALGO_SHIM

    def test_auto_tier_without_pqcrypto_is_shim(self):
        """auto tier falls back to shim when pqcrypto is not installed."""
        if _HAS_PQCRYPTO:
            pytest.skip("pqcrypto is installed — cannot test fallback")
        signer = PQCAttestationSigner("auto-tier-key", tier="auto")
        assert signer.algorithm == _ALGO_SHIM

    def test_tier2_without_pqcrypto_raises(self):
        """Requesting tier2 without pqcrypto must raise RuntimeError."""
        if _HAS_PQCRYPTO:
            pytest.skip("pqcrypto is installed — cannot test error path")
        with pytest.raises(RuntimeError, match="pqcrypto"):
            PQCAttestationSigner("key-material", tier="tier2")


@pytest.mark.skipif(not _HAS_PQCRYPTO, reason="requires pqcrypto (real ML-DSA)")
class TestTier2RealMLDSA:
    """Real NIST ML-DSA-65 signatures. These are the tests that would have
    caught the two shipped bugs: the wrong import name (dilithium3 vs ml_dsa_65)
    and the verify() that ignored its result and always returned True."""

    def test_real_algorithm_is_ml_dsa(self):
        from orchestrator.core.pqc_signer import _ALGO_DILITHIUM
        signer = PQCAttestationSigner("ab" * 32, tier="real")
        assert signer.algorithm == _ALGO_DILITHIUM  # "ml-dsa-65"

    def test_real_sign_verify_roundtrip(self):
        signer = PQCAttestationSigner("ab" * 32, tier="real")
        r = signer.sign(_payload())
        assert r["pqc_algorithm"] == "ml-dsa-65"
        assert signer.verify(_payload(), r["pqc_signature"], r["pqc_algorithm"]) is True

    def test_real_rejects_tampered_payload(self):
        signer = PQCAttestationSigner("ab" * 32, tier="real")
        r = signer.sign('{"amount": 100}')
        # THE regression test: a different payload must NOT verify.
        assert signer.verify('{"amount": 999}', r["pqc_signature"], r["pqc_algorithm"]) is False

    def test_real_rejects_garbage_signature(self):
        signer = PQCAttestationSigner("ab" * 32, tier="real")
        r = signer.sign(_payload())
        assert signer.verify(_payload(), "deadbeef", r["pqc_algorithm"]) is False

    def test_real_rejects_wrong_key(self):
        s1 = PQCAttestationSigner("ab" * 32, tier="real")
        s2 = PQCAttestationSigner("cd" * 32, tier="real")
        r = s1.sign(_payload())
        # s2 has a different (independently generated) keypair.
        assert s2.verify(_payload(), r["pqc_signature"], r["pqc_algorithm"]) is False


# ── PQCAttestationSigner: Key ID and Epoch Rotation ─────────────────────────


class TestPQCKeyAndEpoch:
    """Public key ID generation and epoch-based key rotation."""

    def test_public_key_id_is_stable(self):
        """Same key material must produce the same public_key_id."""
        s1 = _signer("stable-key-test")
        s2 = _signer("stable-key-test")
        assert s1.get_public_key_id() == s2.get_public_key_id()

    def test_public_key_id_is_32_hex(self):
        """Public key ID must be a 32-character hex string."""
        signer = _signer()
        kid = signer.get_public_key_id()
        assert len(kid) == 32
        int(kid, 16)  # must be valid hex

    def test_different_epochs_produce_different_keys(self):
        """Epoch key derivation must produce distinct keys for different epochs."""
        master = b"master-key-for-epoch-test-16-b!"
        k1 = _derive_epoch_key(master, epoch=1)
        k2 = _derive_epoch_key(master, epoch=2)
        assert k1 != k2

    def test_empty_key_raises(self):
        """Constructing a signer with an empty key must raise ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            PQCAttestationSigner("", tier="tier1")
