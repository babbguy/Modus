"""
Tests for orchestrator.core.attestation_engine and orchestrator.core.merkle —
HMAC signing, verification, HKDF derivation, key resolution, Merkle tree
construction and proof verification, and batch signing.
"""

from __future__ import annotations

import hashlib

import pytest

from orchestrator.core.attestation_engine import (
    AttestationSigner,
    _hkdf_derive,
    batch_sign,
    resolve_attestation_key,
)
from orchestrator.core.merkle import MerkleTree


# ── Helpers ──────────────────────────────────────────────────────────────────


def _signer(key: bytes = b"test-key-at-least-16-bytes-long!") -> AttestationSigner:
    return AttestationSigner(key)


def _decision(**overrides) -> dict:
    defaults = {
        "decision_id": "dec-001",
        "decision": "allow",
        "policy_id": "pol-1",
        "policy_type": "budget",
        "app_id": "app1",
        "team_id": "team1",
        "timestamp": "2026-03-13T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


# ── AttestationSigner ───────────────────────────────────────────────────────


class TestAttestationSigner:
    def test_attestation_signer_sign_and_verify(self):
        signer = _signer()
        payload = signer.sign_decision(**_decision())
        sig = payload.pop("signature")
        assert signer.verify_attestation(payload, sig) is True

    def test_attestation_signer_tamper_detection(self):
        signer = _signer()
        payload = signer.sign_decision(**_decision())
        sig = payload.pop("signature")
        payload["decision"] = "deny"  # tamper
        assert signer.verify_attestation(payload, sig) is False

    def test_attestation_signer_key_minimum_length(self):
        with pytest.raises(ValueError, match="at least 16 bytes"):
            AttestationSigner(b"short")


# ── HKDF ────────────────────────────────────────────────────────────────────


class TestHKDF:
    def test_hkdf_derive_deterministic(self):
        key1 = _hkdf_derive(b"my-secret-key")
        key2 = _hkdf_derive(b"my-secret-key")
        assert key1 == key2
        assert len(key1) == 32


# ── Key Resolution ──────────────────────────────────────────────────────────


class TestResolveAttestationKey:
    def test_resolve_attestation_key_from_env(self, monkeypatch):
        # 32-byte hex string
        hex_key = "aa" * 32
        monkeypatch.setenv("MODUS_ATTESTATION_KEY", hex_key)
        monkeypatch.delenv("MODUS_ENCRYPTION_KEY", raising=False)
        result = resolve_attestation_key()
        assert result == bytes.fromhex(hex_key)

    def test_resolve_attestation_key_from_encryption(self, monkeypatch):
        monkeypatch.delenv("MODUS_ATTESTATION_KEY", raising=False)
        monkeypatch.setenv("MODUS_ENCRYPTION_KEY", "my-encryption-key")
        result = resolve_attestation_key()
        assert result is not None
        assert len(result) == 32

    def test_resolve_attestation_key_none(self, monkeypatch):
        monkeypatch.delenv("MODUS_ATTESTATION_KEY", raising=False)
        monkeypatch.delenv("MODUS_ENCRYPTION_KEY", raising=False)
        result = resolve_attestation_key()
        assert result is None


# ── Merkle Tree ─────────────────────────────────────────────────────────────


class TestMerkleTree:
    def test_merkle_tree_single_leaf(self):
        tree = MerkleTree()
        leaf = _sha256_hex("leaf-0")
        tree.add_leaf(leaf)
        root = tree.build()
        # Single leaf: root equals the leaf hash itself
        assert root == leaf

    def test_merkle_tree_two_leaves(self):
        tree = MerkleTree()
        leaf0 = _sha256_hex("leaf-0")
        leaf1 = _sha256_hex("leaf-1")
        tree.add_leaf(leaf0)
        tree.add_leaf(leaf1)
        root = tree.build()

        proof0 = tree.get_proof(0)
        proof1 = tree.get_proof(1)
        assert MerkleTree.verify_proof(leaf0, proof0, root) is True
        assert MerkleTree.verify_proof(leaf1, proof1, root) is True

    def test_merkle_tree_odd_leaves(self):
        tree = MerkleTree()
        leaves = [_sha256_hex(f"leaf-{i}") for i in range(3)]
        for leaf in leaves:
            tree.add_leaf(leaf)
        root = tree.build()

        for i, leaf in enumerate(leaves):
            proof = tree.get_proof(i)
            assert MerkleTree.verify_proof(leaf, proof, root) is True

    def test_merkle_tree_many_leaves(self):
        tree = MerkleTree()
        leaves = [_sha256_hex(f"leaf-{i}") for i in range(10)]
        for leaf in leaves:
            tree.add_leaf(leaf)
        root = tree.build()

        for i, leaf in enumerate(leaves):
            proof = tree.get_proof(i)
            assert MerkleTree.verify_proof(leaf, proof, root) is True

    def test_merkle_tree_verify_proof(self):
        tree = MerkleTree()
        leaves = [_sha256_hex(f"data-{i}") for i in range(4)]
        for leaf in leaves:
            tree.add_leaf(leaf)
        root = tree.build()

        proof = tree.get_proof(2)
        assert MerkleTree.verify_proof(leaves[2], proof, root) is True
        # Wrong leaf should fail
        assert MerkleTree.verify_proof(leaves[0], proof, root) is False

    def test_merkle_tree_invalid_leaf(self):
        tree = MerkleTree()
        with pytest.raises(ValueError):
            tree.add_leaf("not-a-valid-hex-string-of-64-chars")


# ── Batch Sign ──────────────────────────────────────────────────────────────


class TestBatchSign:
    def test_batch_sign(self):
        signer = _signer()
        decisions = [
            _decision(decision_id=f"dec-{i}") for i in range(5)
        ]
        attestations, merkle_root = batch_sign(signer, decisions)

        assert len(attestations) == 5
        assert len(merkle_root) == 64  # SHA-256 hex

        for att in attestations:
            assert "signature" in att
            assert "merkle_proof" in att
            assert att["merkle_root"] == merkle_root

    def test_batch_sign_empty(self):
        signer = _signer()
        with pytest.raises(ValueError, match="empty"):
            batch_sign(signer, [])
