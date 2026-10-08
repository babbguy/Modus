"""
Modus — Threshold Approval Engine (Preview)
==========================================
Threshold (k-of-n) approval for joint policy sign-off. Includes a real
Shamir secret-sharing primitive over GF(2^31-1).

IMPORTANT — this is NOT privacy-preserving multi-party computation (MPC).
There is no secure multi-party protocol here: in the additive mode the
central evaluator receives each party's contribution in plaintext, and even
in the Shamir mode a single evaluator reconstructs the secret. It provides
k-of-n threshold sign-off, not input privacy. Treat it as a Preview feature.

Pure Python, stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Optional


logger = logging.getLogger(__name__)

# Mersenne prime for GF(2^31-1)
_PRIME = (1 << 31) - 1


class ShamirSecretSharing:
    """Shamir's Secret Sharing over GF(2^31-1)."""

    @staticmethod
    def split(secret: int, threshold_k: int, total_n: int) -> list[tuple[int, int]]:
        """Split a secret into n shares with threshold k."""
        if threshold_k > total_n:
            raise ValueError(f"Threshold {threshold_k} cannot exceed total parties {total_n}")
        if secret < 0 or secret >= _PRIME:
            secret = secret % _PRIME

        # Generate random polynomial coefficients
        coefficients = [secret]
        for _ in range(threshold_k - 1):
            coefficients.append(secrets.randbelow(_PRIME))

        # Evaluate polynomial at points 1..n
        shares = []
        for x in range(1, total_n + 1):
            y = 0
            for i, coeff in enumerate(coefficients):
                y = (y + coeff * pow(x, i, _PRIME)) % _PRIME
            shares.append((x, y))

        return shares

    @staticmethod
    def reconstruct(shares: list[tuple[int, int]], threshold_k: int) -> int:
        """Reconstruct the secret from k or more shares using Lagrange interpolation."""
        if len(shares) < threshold_k:
            raise ValueError(f"Need at least {threshold_k} shares, got {len(shares)}")

        # Use only threshold_k shares
        shares = shares[:threshold_k]

        secret = 0
        for i, (xi, yi) in enumerate(shares):
            numerator = 1
            denominator = 1
            for j, (xj, _) in enumerate(shares):
                if i != j:
                    numerator = (numerator * (-xj)) % _PRIME
                    denominator = (denominator * (xi - xj)) % _PRIME

            # Modular inverse
            lagrange = (numerator * pow(denominator, _PRIME - 2, _PRIME)) % _PRIME
            secret = (secret + yi * lagrange) % _PRIME

        return secret


class MPCPolicyEvaluator:
    """
    Threshold (k-of-n) policy sign-off evaluator (Preview).

    NOT privacy-preserving MPC. In ``additive`` mode each party's
    contribution is submitted and read in plaintext by this central
    evaluator; in ``shamir`` mode this single evaluator reconstructs the
    secret from the shares. It enforces a k-of-n threshold, not input
    privacy.
    """

    def __init__(self, threshold_k: int, total_n: int, mode: str = "additive"):
        self.threshold_k = threshold_k
        self.total_n = total_n
        self.mode = mode  # "additive" or "shamir"

    def evaluate_additive(self, contributions: list[dict]) -> dict:
        """Additive aggregation: sum plaintext contributions (not private MPC)."""
        if len(contributions) < self.threshold_k:
            return {"decision": "pending", "reason": f"Need {self.threshold_k} contributions, got {len(contributions)}"}

        # Sum policy scores
        total_score = 0
        total_budget = 0
        total_spend = 0

        for contrib in contributions:
            data = contrib.get("data", {})
            total_score += data.get("policy_score", 0)
            total_budget += data.get("budget_share", 0)
            total_spend += data.get("spend_share", 0)

        # Decision: allow if aggregate score is positive and within budget
        avg_score = total_score / len(contributions)
        decision = "allow" if avg_score > 0 and total_spend <= total_budget else "deny"

        return {
            "decision": decision,
            "party_count": len(contributions),
            "threshold_met": len(contributions) >= self.threshold_k,
            "aggregate_score": avg_score,
        }

    def evaluate_shamir(self, shares: list[tuple[int, int]]) -> dict:
        """Shamir mode: a single evaluator reconstructs the secret from shares
        (threshold sign-off; not a private multi-party protocol)."""
        try:
            secret = ShamirSecretSharing.reconstruct(shares, self.threshold_k)
            # Interpret reconstructed value: positive = allow, 0 = deny
            decision = "allow" if secret > 0 else "deny"
            return {
                "decision": decision,
                "party_count": len(shares),
                "threshold_met": True,
                "reconstructed_value": secret,
            }
        except ValueError as e:
            return {
                "decision": "pending",
                "reason": str(e),
                "threshold_met": False,
            }

    def evaluate(self, contributions: list) -> dict:
        """Evaluate using configured mode."""
        if self.mode == "shamir":
            shares = [(c.get("x", 0), c.get("y", 0)) for c in contributions]
            return self.evaluate_shamir(shares)
        return self.evaluate_additive(contributions)


def generate_party_hash(party_id: str, swarm_salt: str) -> str:
    """Generate one-way HMAC-SHA256 hash for a party. No reversible identifiers."""
    return hmac.new(
        swarm_salt.encode(),
        party_id.encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_zk_proof(mpc_result: dict) -> Optional[str]:
    """Return a PLACEHOLDER attestation for a threshold-approval result.

    This is NOT a real zero-knowledge proof. It allocates two public inputs
    into an empty circuit (which has no constraints, so ``check_constraints``
    trivially passes) and returns a constant JSON blob echoing the inputs.
    It proves nothing to a third party; retained only as a placeholder.
    """
    try:
        from orchestrator.core.zk_circuit import ArithmeticCircuit
        circuit = ArithmeticCircuit()

        party_count = mpc_result.get("party_count", 0)
        decision_val = 1 if mpc_result.get("decision") == "allow" else 0

        circuit.allocate_public_input("party_count", party_count)
        circuit.allocate_public_input("decision", decision_val)

        if circuit.check_constraints():
            return json.dumps({
                "proof_type": "mpc_evaluation",
                "party_count": party_count,
                "decision_verified": True,
            })
    except Exception:
        pass
    return None
