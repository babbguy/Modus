"""
Modus — Trajectory Compliance Receipt Generator (Phase 8b)
==============================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Generates tamper-evident compliance receipts for agent sessions.  A receipt
binds a sequence of AI agent actions (trajectory) and the active governance
policies into a single commitment, so that later tampering with the recorded
trajectory or policy set is detectable.

NOTE ON CLAIMS: This is NOT a zero-knowledge proof.  The receipt is a
SHA-256 hash-chain commitment.  It does not hide the trajectory from a
verifier (the public inputs contain aggregate cost/token/call figures), and
it proves nothing to a third party who does not already hold the underlying
data.  It provides integrity/tamper-evidence, not zero-knowledge.

Two tiers:

  Tier 1 — Hash-Chain (pure Python, no dependencies)
      Computes a deterministic SHA-256 hash-chain commitment binding policy
      hashes, trajectory step hashes, and outcome data.  Verification
      re-derives the chain from public inputs.  Commitment size: 64 bytes.

  Tier 2 — EXPERIMENTAL pairing tautology (optional py_ecc)
      Builds an ArithmeticCircuit encoding policy constraints (budget,
      rate limits, token caps) and produces a pairing-based value over the
      BN128 curve.  This is experimental and is NOT a production zk-SNARK:
      the pairing check is a self-consistent tautology, not a soundness
      guarantee against a malicious prover.  Falls back to Tier 1 if
      py_ecc is not installed.

All computation runs locally.  No data leaves the customer's
infrastructure.  Thread-safe — no shared mutable state between receipts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional

from orchestrator.core.zk_circuit import (
    ArithmeticCircuit,
)

logger = logging.getLogger(__name__)

# ── Check for optional py_ecc dependency ────────────────────────────────────

_pyecc_available = False
try:
    from py_ecc.bn128 import (  # type: ignore[import-untyped]
        G1,
        G2,
        multiply as ec_multiply,
        add as ec_add,
        pairing,
        neg as ec_neg,
        curve_order,
        Z1,
    )
    _pyecc_available = True
except ImportError:
    G1 = G2 = ec_multiply = ec_add = pairing = ec_neg = curve_order = Z1 = None


# ── Result dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrajectoryProofResult:
    """Immutable result of a trajectory proof generation."""
    session_id: str
    proof_type: str         # "hash_chain" or "snark"
    proof_status: str       # "valid" | "invalid" | "error"
    proof_data: str         # hex-encoded proof bytes
    public_inputs: dict     # verifier-visible data
    circuit_size: int       # number of R1CS constraints (0 for hash-chain)
    prover_time_ms: int     # wall-clock proving time
    created_at: str         # ISO-8601


# ── Helpers ─────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    """Return hex-encoded SHA-256 digest."""
    return hashlib.sha256(data).hexdigest()


def _sha256_str(text: str) -> str:
    """Return hex-encoded SHA-256 of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _hash_step(step: dict) -> str:
    """Hash a single trajectory step deterministically."""
    return _sha256_str(_canonical_json(step))


def _hash_policy(policy: dict) -> str:
    """Hash a single policy deterministically."""
    return _sha256_str(_canonical_json(policy))


# ── LRU Cache ───────────────────────────────────────────────────────────────

class _ProofCache:
    """
    Thread-safe LRU cache for trajectory proofs.

    Keyed by SHA-256(session_id || sorted_policy_hashes || trajectory_hash).
    Bounded to ``max_size`` entries.
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._max_size = max_size
        self._cache: OrderedDict[str, TrajectoryProofResult] = OrderedDict()
        self._lock = threading.Lock()

    def _make_key(
        self,
        session_id: str,
        policy_hashes: List[str],
        trajectory_hash: str,
    ) -> str:
        key_material = session_id + "||" + ",".join(sorted(policy_hashes)) + "||" + trajectory_hash
        return _sha256_str(key_material)

    def get(
        self,
        session_id: str,
        policy_hashes: List[str],
        trajectory_hash: str,
    ) -> Optional[TrajectoryProofResult]:
        key = self._make_key(session_id, policy_hashes, trajectory_hash)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def put(
        self,
        session_id: str,
        policy_hashes: List[str],
        trajectory_hash: str,
        result: TrajectoryProofResult,
    ) -> None:
        key = self._make_key(session_id, policy_hashes, trajectory_hash)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                if len(self._cache) >= self._max_size:
                    self._cache.popitem(last=False)
            self._cache[key] = result

    def clear_session(self, session_id: str) -> int:
        """Remove all cached proofs containing the given session_id. Returns count removed."""
        removed = 0
        with self._lock:
            keys_to_remove = [
                k for k, v in self._cache.items()
                if v.session_id == session_id
            ]
            for k in keys_to_remove:
                del self._cache[k]
                removed += 1
        return removed

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


# ── Tier 1: Hash-Chain Prover ───────────────────────────────────────────────

def _prove_hash_chain(
    session_id: str,
    trajectory_steps: List[dict],
    active_policies: List[dict],
) -> TrajectoryProofResult:
    """
    Generate a hash-chain trajectory proof (Tier 1).

    Proof construction:
        1. Compute ``policy_digest  = SHA-256(sorted policy hashes)``
        2. Compute ``step_digest    = SHA-256(ordered step hashes)``
        3. Compute ``outcome_digest = SHA-256(aggregate outcome data)``
        4. Final proof = ``SHA-256(policy_digest || step_digest || outcome_digest)``

    The proof binds session identity, policy set, and trajectory into a
    single 256-bit commitment.  Verification re-derives the chain from
    the same inputs.
    """
    t0 = time.perf_counter()

    # Compute policy digest
    policy_hashes = sorted([_hash_policy(p) for p in active_policies])
    policy_digest = _sha256("".join(policy_hashes).encode("utf-8"))

    # Compute step digest (order-preserving)
    step_hashes = [_hash_step(s) for s in trajectory_steps]
    step_digest = _sha256("".join(step_hashes).encode("utf-8"))

    # Compute outcome digest (aggregate metrics from trajectory)
    total_cost = sum(float(s.get("cost_usd", 0)) for s in trajectory_steps)
    total_tokens = sum(int(s.get("tokens", 0)) for s in trajectory_steps)
    total_calls = len(trajectory_steps)
    models_used = sorted(set(s.get("model", "unknown") for s in trajectory_steps))

    outcome_data = _canonical_json({
        "session_id": session_id,
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "total_calls": total_calls,
        "models_used": models_used,
    })
    outcome_digest = _sha256_str(outcome_data)

    # Final proof: SHA-256(policy_digest || step_digest || outcome_digest)
    proof_input = bytes.fromhex(policy_digest) + bytes.fromhex(step_digest) + bytes.fromhex(outcome_digest)
    proof_hex = _sha256(proof_input)

    # Check policy compliance
    compliance_ok = _check_trajectory_compliance(
        trajectory_steps, active_policies,
    )

    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    public_inputs = {
        "session_id": session_id,
        "policy_digest": policy_digest,
        "step_digest": step_digest,
        "outcome_digest": outcome_digest,
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "total_calls": total_calls,
        "models_used": models_used,
        "policy_count": len(active_policies),
        "compliant": compliance_ok,
    }

    return TrajectoryProofResult(
        session_id=session_id,
        proof_type="hash_chain",
        proof_status="valid" if compliance_ok else "invalid",
        proof_data=proof_hex,
        public_inputs=public_inputs,
        circuit_size=0,
        prover_time_ms=elapsed_ms,
        created_at=_now_iso(),
    )


# ── Tier 2: Experimental pairing-based commitment ───────────────────────────

def _prove_snark(
    session_id: str,
    trajectory_steps: List[dict],
    active_policies: List[dict],
) -> TrajectoryProofResult:
    """
    Generate an EXPERIMENTAL pairing-based commitment (Tier 2).

    Builds an ArithmeticCircuit encoding policy constraints as R1CS,
    then produces a tuple (A, B, C) over the BN128 curve.  This is NOT a
    production zk-SNARK — the resulting pairing relation is a tautology
    (A, B, C are derived from the same scalars), so it carries no
    soundness guarantee against a malicious prover.  It is retained for
    experimentation only.  Falls back to Tier 1 if py_ecc is unavailable.
    """
    if not _pyecc_available:
        logger.debug("py_ecc not available, falling back to hash-chain prover")
        return _prove_hash_chain(session_id, trajectory_steps, active_policies)

    t0 = time.perf_counter()

    try:
        # Build circuit
        circuit = ArithmeticCircuit()

        # Aggregate trajectory metrics as public inputs
        total_cost_cents = sum(
            round(float(s.get("cost_usd", 0)) * 100) for s in trajectory_steps
        )
        total_tokens = sum(int(s.get("tokens", 0)) for s in trajectory_steps)
        total_calls = len(trajectory_steps)

        cost_var = circuit.allocate_public_input("total_cost_cents", total_cost_cents)
        tokens_var = circuit.allocate_public_input("total_tokens", total_tokens)
        calls_var = circuit.allocate_public_input("total_calls", total_calls)

        # Encode each policy as circuit constraints
        for policy in active_policies:
            ptype = policy.get("type", "")
            config = policy.get("config", {}) or {}

            if ptype == "budget_cap":
                cap_cents = round(float(config.get("cap_usd", 0)) * 100)
                if cap_cents > 0:
                    cap_var = circuit.allocate_variable(
                        f"budget_cap_{id(policy)}", cap_cents,
                    )
                    # cost <= cap  =>  cost + slack = cap
                    circuit.assert_less_equal(cost_var, cap_var)

            elif ptype == "rate_limit":
                max_calls = int(config.get("max_calls", 0))
                if max_calls > 0:
                    limit_var = circuit.allocate_variable(
                        f"rate_limit_{id(policy)}", max_calls,
                    )
                    circuit.assert_less_equal(calls_var, limit_var)

            elif ptype == "token_cap":
                max_tokens = int(config.get("max_tokens", 0))
                if max_tokens > 0:
                    token_limit_var = circuit.allocate_variable(
                        f"token_cap_{id(policy)}", max_tokens,
                    )
                    circuit.assert_less_equal(tokens_var, token_limit_var)

        # Verify the circuit witness satisfies all constraints
        constraints_ok = circuit.check_constraints()

        # Generate elements using BN128 curve (EXPERIMENTAL, not a real SNARK).
        # tuple = (A, B, C) where
        #   A = r * G1
        #   B = s * G2
        #   C = (r * s) * G1
        # with r, s derived from witness hash for determinism. Because C is
        # built from the same r, s, the pairing check e(A,B)==e(C,G2) always
        # holds regardless of the witness — a tautology, not a soundness proof.
        witness_hash = _sha256_str(
            session_id + _canonical_json([s for s in trajectory_steps])
        )
        r = int(witness_hash[:32], 16) % curve_order
        s = int(witness_hash[32:], 16) % curve_order
        if r == 0:
            r = 1
        if s == 0:
            s = 1

        proof_A = ec_multiply(G1, r)
        proof_B = ec_multiply(G2, s)
        proof_C = ec_multiply(G1, (r * s) % curve_order)

        # Serialize proof elements as hex
        def _point_to_hex(point: Any) -> str:
            """Serialize an elliptic curve point to hex."""
            if point is None or point == Z1:
                return "00"
            x, y = point
            return format(int(x), "064x") + format(int(y), "064x")

        def _g2_point_to_hex(point: Any) -> str:
            """Serialize a G2 point (has nested tuples) to hex."""
            if point is None:
                return "00"
            (x1, x2), (y1, y2) = point
            return (
                format(int(x1), "064x") + format(int(x2), "064x")
                + format(int(y1), "064x") + format(int(y2), "064x")
            )

        proof_data = (
            _point_to_hex(proof_A)
            + _g2_point_to_hex(proof_B)
            + _point_to_hex(proof_C)
        )

        # Policy/step digests for public inputs (same as hash-chain)
        policy_hashes = sorted([_hash_policy(p) for p in active_policies])
        policy_digest = _sha256("".join(policy_hashes).encode("utf-8"))
        step_hashes = [_hash_step(s) for s in trajectory_steps]
        step_digest = _sha256("".join(step_hashes).encode("utf-8"))

        elapsed_ms = round((time.perf_counter() - t0) * 1000)

        total_cost_usd = total_cost_cents / 100.0

        public_inputs = {
            "session_id": session_id,
            "policy_digest": policy_digest,
            "step_digest": step_digest,
            "total_cost_usd": total_cost_usd,
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "policy_count": len(active_policies),
            "compliant": constraints_ok,
            "curve": "bn128",
        }

        return TrajectoryProofResult(
            session_id=session_id,
            proof_type="snark",
            proof_status="valid" if constraints_ok else "invalid",
            proof_data=proof_data,
            public_inputs=public_inputs,
            circuit_size=circuit.constraint_count,
            prover_time_ms=elapsed_ms,
            created_at=_now_iso(),
        )

    except Exception as exc:
        logger.warning("SNARK prover failed, falling back to hash-chain: %s", exc)
        return _prove_hash_chain(session_id, trajectory_steps, active_policies)


# ── Policy compliance checker ───────────────────────────────────────────────

def _check_trajectory_compliance(
    trajectory_steps: List[dict],
    active_policies: List[dict],
) -> bool:
    """
    Check whether the trajectory complies with all active policies.

    This is the plaintext compliance check used by the hash-chain prover.
    The SNARK prover encodes these same checks as circuit constraints.

    Returns True if all policies are satisfied.
    """
    total_cost = sum(float(s.get("cost_usd", 0)) for s in trajectory_steps)
    total_tokens = sum(int(s.get("tokens", 0)) for s in trajectory_steps)
    total_calls = len(trajectory_steps)

    for policy in active_policies:
        ptype = policy.get("type", "")
        config = policy.get("config", {}) or {}

        if ptype == "budget_cap":
            cap = float(config.get("cap_usd", 0))
            if cap > 0 and total_cost > cap:
                return False

        elif ptype == "rate_limit":
            max_calls = int(config.get("max_calls", 0))
            if max_calls > 0 and total_calls > max_calls:
                return False

        elif ptype == "token_cap":
            max_tokens = int(config.get("max_tokens", 0))
            if max_tokens > 0 and total_tokens > max_tokens:
                return False

    return True


# ── Verification ────────────────────────────────────────────────────────────

def _verify_hash_chain(proof_data: str, public_inputs: dict) -> bool:
    """
    Verify a hash-chain proof by recomputing from public inputs.

    Re-derives ``SHA-256(policy_digest || step_digest || outcome_digest)``
    and compares to the proof.
    """
    policy_digest = public_inputs.get("policy_digest", "")
    step_digest = public_inputs.get("step_digest", "")
    outcome_digest = public_inputs.get("outcome_digest", "")

    if not all([policy_digest, step_digest, outcome_digest]):
        return False

    try:
        proof_input = (
            bytes.fromhex(policy_digest)
            + bytes.fromhex(step_digest)
            + bytes.fromhex(outcome_digest)
        )
        expected = _sha256(proof_input)
        return expected == proof_data
    except (ValueError, TypeError):
        return False


def _verify_snark(proof_data: str, public_inputs: dict) -> bool:
    """
    Check the EXPERIMENTAL pairing-based commitment (Tier 2).

    Recomputes the pairing relation:
        e(A, B) == e(C, G2)
    where A, B, C are extracted from the serialized commitment.  Note this
    relation is a tautology by construction (see ``_prove_snark``); passing
    it confirms the commitment is well-formed, not that a hidden witness
    satisfied the policies.  This is NOT zk-SNARK verification.

    Falls back to checking the ``compliant`` flag if py_ecc is unavailable.
    """
    if not _pyecc_available:
        logger.debug("py_ecc not available for SNARK verification, checking compliance flag")
        return public_inputs.get("compliant", False)

    try:
        # Parse proof_data: A (128 hex) + B (256 hex) + C (128 hex) = 512 hex
        if len(proof_data) < 512:
            return False

        # Extract A (G1 point: 128 hex chars = 2 * 64)
        a_hex = proof_data[:128]
        a_x = int(a_hex[:64], 16)
        a_y = int(a_hex[64:128], 16)
        proof_A = (a_x, a_y)

        # Extract B (G2 point: 256 hex chars = 4 * 64)
        b_hex = proof_data[128:384]
        b_x1 = int(b_hex[:64], 16)
        b_x2 = int(b_hex[64:128], 16)
        b_y1 = int(b_hex[128:192], 16)
        b_y2 = int(b_hex[192:256], 16)
        proof_B = ((b_x1, b_x2), (b_y1, b_y2))

        # Extract C (G1 point: 128 hex chars)
        c_hex = proof_data[384:512]
        c_x = int(c_hex[:64], 16)
        c_y = int(c_hex[64:128], 16)
        proof_C = (c_x, c_y)

        # Pairing check: e(A, B) == e(C, G2)
        lhs = pairing(proof_B, proof_A)
        rhs = pairing(G2, proof_C)

        return lhs == rhs

    except Exception as exc:
        logger.warning("SNARK verification failed: %s", exc)
        return False


# ── TrajectoryProver ────────────────────────────────────────────────────────

class TrajectoryProver:
    """
    Trajectory compliance-receipt generator for agent sessions.

    Generates tamper-evident compliance receipts (SHA-256 hash chain) that
    bind an agent's trajectory (sequence of AI calls with costs, tokens, and
    tool usage) to the active governance policies, so recorded results cannot
    be silently altered.  This is not a zero-knowledge proof and reveals
    aggregate figures in its public inputs (see module docstring).

    Usage::

        prover = TrajectoryProver()

        steps = [
            {"call_idx": 0, "model": "gpt-4", "cost_usd": 0.05,
             "tokens": 500, "tool_calls": ["db_query"]},
            {"call_idx": 1, "model": "gpt-4", "cost_usd": 0.03,
             "tokens": 300, "tool_calls": []},
        ]
        policies = [
            {"type": "budget_cap", "config": {"cap_usd": 1.00}},
            {"type": "rate_limit", "config": {"max_calls": 100}},
        ]

        result = prover.generate_proof("session-123", steps, policies)
        assert result.proof_status == "valid"
        assert prover.verify_proof(result.proof_data, result.public_inputs)

    Parameters
    ----------
    tier : str
        Proving tier: ``"auto"`` (default), ``"hash_chain"``, or ``"snark"``.
        ``"auto"`` uses SNARK if py_ecc is installed, hash-chain otherwise.
    cache_size : int
        Maximum number of cached proof results (default 1000).
    """

    def __init__(self, tier: str = "auto", cache_size: int = 1000) -> None:
        if tier == "auto":
            self._tier = "snark" if _pyecc_available else "hash_chain"
        elif tier in ("hash_chain", "snark"):
            self._tier = tier
        else:
            raise ValueError(f"Unknown tier {tier!r}. Use 'auto', 'hash_chain', or 'snark'.")

        if self._tier == "snark" and not _pyecc_available:
            logger.info(
                "SNARK tier requested but py_ecc not installed. "
                "Falling back to hash_chain."
            )
            self._tier = "hash_chain"

        self._cache = _ProofCache(max_size=cache_size)
        logger.info("TrajectoryProver initialized with tier=%s", self._tier)

    @property
    def tier(self) -> str:
        """Active proving tier."""
        return self._tier

    @property
    def cache_size(self) -> int:
        """Current number of cached proofs."""
        return self._cache.size

    def generate_proof(
        self,
        session_id: str,
        trajectory_steps: List[dict],
        active_policies: List[dict],
    ) -> TrajectoryProofResult:
        """
        Generate a trajectory compliance receipt for an agent session.

        Parameters
        ----------
        session_id : str
            Unique identifier for the agent session.
        trajectory_steps : list[dict]
            Ordered list of trajectory steps. Each step should contain:
            ``call_idx``, ``model``, ``cost_usd``, ``tokens``, ``tool_calls``.
        active_policies : list[dict]
            List of active governance policies. Each should contain:
            ``type`` and ``config``.

        Returns
        -------
        TrajectoryProofResult
            Frozen dataclass with proof data, public inputs, and timing.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")

        # Check cache
        policy_hashes = [_hash_policy(p) for p in active_policies]
        trajectory_hash = _sha256_str(
            _canonical_json(trajectory_steps)
        )

        cached = self._cache.get(session_id, policy_hashes, trajectory_hash)
        if cached is not None:
            logger.debug("Cache hit for session %s", session_id)
            return cached

        # Generate proof
        if self._tier == "snark":
            result = _prove_snark(session_id, trajectory_steps, active_policies)
        else:
            result = _prove_hash_chain(session_id, trajectory_steps, active_policies)

        # Cache result
        self._cache.put(session_id, policy_hashes, trajectory_hash, result)

        return result

    def verify_proof(
        self,
        proof_data: str,
        public_inputs: dict,
    ) -> bool:
        """
        Verify a trajectory compliance receipt (recompute the commitment).

        Parameters
        ----------
        proof_data : str
            Hex-encoded commitment (from ``TrajectoryProofResult.proof_data``).
        public_inputs : dict
            Public inputs (from ``TrajectoryProofResult.public_inputs``).

        Returns
        -------
        bool
            True if the proof is valid.
        """
        if not proof_data or not public_inputs:
            return False

        # Determine proof type from public inputs
        curve = public_inputs.get("curve")
        if curve == "bn128":
            return _verify_snark(proof_data, public_inputs)
        else:
            return _verify_hash_chain(proof_data, public_inputs)

    def clear_session_cache(self, session_id: str) -> int:
        """
        Remove all cached proofs for a session (e.g. when session ends).

        Returns the number of entries removed.
        """
        return self._cache.clear_session(session_id)

    def clear_cache(self) -> None:
        """Clear the entire proof cache."""
        self._cache.clear()

    def __repr__(self) -> str:
        return (
            f"<TrajectoryProver tier={self._tier!r} "
            f"cached={self._cache.size}>"
        )
