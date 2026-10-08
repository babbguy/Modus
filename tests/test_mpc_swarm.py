"""
Tests for orchestrator.core.mpc_engine and orchestrator.core.pdr_generator —
Shamir secret sharing, MPC policy evaluation, party hashing, PDR generation.
"""
from __future__ import annotations

import pytest

from orchestrator.core.mpc_engine import (
    ShamirSecretSharing,
    MPCPolicyEvaluator,
    generate_party_hash,
    _PRIME,
)
from orchestrator.core.pdr_generator import generate_pdr, verify_pdr


class TestShamirSecretSharing:
    """Shamir's Secret Sharing over GF(2^31-1)."""

    def test_split_and_reconstruct(self):
        """Basic split-reconstruct round trip."""
        secret = 42
        shares = ShamirSecretSharing.split(secret, threshold_k=2, total_n=3)
        assert len(shares) == 3
        reconstructed = ShamirSecretSharing.reconstruct(shares[:2], threshold_k=2)
        assert reconstructed == secret

    def test_reconstruct_with_any_k_shares(self):
        """Any k shares should reconstruct the secret."""
        secret = 12345
        shares = ShamirSecretSharing.split(secret, threshold_k=3, total_n=5)

        # Try different combinations of 3 shares
        import itertools
        for combo in itertools.combinations(shares, 3):
            result = ShamirSecretSharing.reconstruct(list(combo), threshold_k=3)
            assert result == secret

    def test_insufficient_shares_raises(self):
        """Fewer than k shares should raise ValueError."""
        shares = ShamirSecretSharing.split(42, threshold_k=3, total_n=5)
        with pytest.raises(ValueError, match="Need at least"):
            ShamirSecretSharing.reconstruct(shares[:2], threshold_k=3)

    def test_threshold_exceeds_total_raises(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            ShamirSecretSharing.split(42, threshold_k=5, total_n=3)

    def test_large_secret(self):
        secret = _PRIME - 1
        shares = ShamirSecretSharing.split(secret, threshold_k=2, total_n=3)
        result = ShamirSecretSharing.reconstruct(shares, threshold_k=2)
        assert result == secret

    def test_zero_secret(self):
        shares = ShamirSecretSharing.split(0, threshold_k=2, total_n=3)
        result = ShamirSecretSharing.reconstruct(shares, threshold_k=2)
        assert result == 0

    def test_k_equals_n(self):
        """All shares required."""
        secret = 999
        shares = ShamirSecretSharing.split(secret, threshold_k=3, total_n=3)
        result = ShamirSecretSharing.reconstruct(shares, threshold_k=3)
        assert result == secret


class TestMPCPolicyEvaluator:
    """MPC policy evaluation."""

    def test_additive_allow(self):
        evaluator = MPCPolicyEvaluator(threshold_k=2, total_n=3, mode="additive")
        contributions = [
            {"data": {"policy_score": 1, "budget_share": 100, "spend_share": 30}},
            {"data": {"policy_score": 1, "budget_share": 100, "spend_share": 40}},
        ]
        result = evaluator.evaluate_additive(contributions)
        assert result["decision"] == "allow"
        assert result["threshold_met"] is True

    def test_additive_deny_over_budget(self):
        evaluator = MPCPolicyEvaluator(threshold_k=2, total_n=3, mode="additive")
        contributions = [
            {"data": {"policy_score": 1, "budget_share": 50, "spend_share": 100}},
            {"data": {"policy_score": 1, "budget_share": 50, "spend_share": 100}},
        ]
        result = evaluator.evaluate_additive(contributions)
        assert result["decision"] == "deny"

    def test_additive_deny_negative_score(self):
        evaluator = MPCPolicyEvaluator(threshold_k=2, total_n=3, mode="additive")
        contributions = [
            {"data": {"policy_score": -1, "budget_share": 100, "spend_share": 10}},
            {"data": {"policy_score": -1, "budget_share": 100, "spend_share": 10}},
        ]
        result = evaluator.evaluate_additive(contributions)
        assert result["decision"] == "deny"

    def test_insufficient_contributions(self):
        evaluator = MPCPolicyEvaluator(threshold_k=3, total_n=5, mode="additive")
        result = evaluator.evaluate_additive([{"data": {"policy_score": 1}}])
        assert result["decision"] == "pending"

    def test_shamir_evaluation(self):
        evaluator = MPCPolicyEvaluator(threshold_k=2, total_n=3, mode="shamir")
        # Create shares of a positive secret (allow)
        shares = ShamirSecretSharing.split(42, threshold_k=2, total_n=3)
        result = evaluator.evaluate_shamir(shares[:2])
        assert result["decision"] == "allow"
        assert result["threshold_met"] is True

    def test_evaluate_dispatches_to_mode(self):
        evaluator = MPCPolicyEvaluator(threshold_k=2, total_n=3, mode="additive")
        contributions = [
            {"data": {"policy_score": 1, "budget_share": 100, "spend_share": 10}},
            {"data": {"policy_score": 1, "budget_share": 100, "spend_share": 10}},
        ]
        result = evaluator.evaluate(contributions)
        assert result["decision"] == "allow"


class TestPartyHash:
    """Party identity hashing."""

    def test_deterministic(self):
        h1 = generate_party_hash("party-1", "salt-abc")
        h2 = generate_party_hash("party-1", "salt-abc")
        assert h1 == h2

    def test_different_parties(self):
        h1 = generate_party_hash("party-1", "salt")
        h2 = generate_party_hash("party-2", "salt")
        assert h1 != h2

    def test_different_salts(self):
        h1 = generate_party_hash("party-1", "salt-1")
        h2 = generate_party_hash("party-1", "salt-2")
        assert h1 != h2

    def test_hash_length(self):
        h = generate_party_hash("party", "salt")
        assert len(h) == 64  # SHA-256 hex


class TestPDRGenerator:
    """Policy Decision Record generation and verification."""

    def test_generate_pdr(self):
        mpc_result = {
            "decision": "allow",
            "party_count": 3,
            "threshold_met": True,
        }
        pdr = generate_pdr("team-1", "swarm-1", "session-1", mpc_result)
        assert pdr["decision"] == "allow"
        assert pdr["party_count"] == 3
        assert pdr["threshold_met"] is True
        assert "issued_at" in pdr

    def test_verify_pdr_proof_only_is_not_valid(self):
        # An mpc_proof alone is NOT cryptographic proof of integrity — validity
        # requires a signature that actually verifies. (Previously verify_pdr
        # returned valid=True on mere proof presence; that was the bug.)
        pdr = {
            "decision": "allow",
            "party_count": 3,
            "threshold_met": True,
            "attestation": None,
            "attestation_signature": None,
            "mpc_proof": '{"proof_type": "mpc_evaluation"}',
        }
        result = verify_pdr(pdr)
        assert result["valid"] is False
        assert result["proof_present"] is True

    def test_verify_pdr_signed_roundtrip(self, monkeypatch):
        # A properly signed PDR verifies; tampering with the decision fails it.
        monkeypatch.setenv("MODUS_ATTESTATION_KEY", "cd" * 32)
        from orchestrator.core.pdr_generator import generate_pdr
        pdr = generate_pdr("t1", "s1", "sess1",
                           {"decision": "allow", "party_count": 3, "threshold_met": True})
        assert verify_pdr(pdr)["valid"] is True
        pdr["decision"] = "deny"
        assert verify_pdr(pdr)["valid"] is False

    def test_verify_pdr_no_proof_no_sig(self):
        pdr = {
            "decision": "deny",
            "party_count": 2,
            "threshold_met": False,
            "attestation_signature": None,
            "mpc_proof": None,
        }
        result = verify_pdr(pdr)
        assert result["valid"] is False
