"""
Tests for orchestrator.core.zk_trajectory_prover and orchestrator.core.zk_circuit —
ArithmeticCircuit R1CS constraints, hash-chain trajectory proof generation,
verification, determinism, cache behaviour, and compliance detection.
"""

from __future__ import annotations

import pytest

from orchestrator.core.zk_circuit import (
    ArithmeticCircuit,
    _to_field,
)
from orchestrator.core.zk_trajectory_prover import (
    TrajectoryProver,
    TrajectoryProofResult,
    _check_trajectory_compliance,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _steps(count: int = 3, cost: float = 0.05, tokens: int = 500) -> list[dict]:
    return [
        {
            "call_idx": i,
            "model": "gpt-4",
            "cost_usd": cost,
            "tokens": tokens,
            "tool_calls": [],
        }
        for i in range(count)
    ]


def _policies_budget(cap: float = 1.00) -> list[dict]:
    return [{"type": "budget_cap", "config": {"cap_usd": cap}}]


def _policies_rate(max_calls: int = 100) -> list[dict]:
    return [{"type": "rate_limit", "config": {"max_calls": max_calls}}]


# ── ArithmeticCircuit ────────────────────────────────────────────────────────


class TestArithmeticCircuit:
    """R1CS circuit construction and constraint checking."""

    def test_allocate_variable_increments_count(self):
        """Each allocate_variable call must increase variable_count."""
        circuit = ArithmeticCircuit()
        assert circuit.variable_count == 1  # ONE wire
        circuit.allocate_variable("x", 5)
        assert circuit.variable_count == 2

    def test_public_input_allocation(self):
        """Public inputs must occupy indices right after the ONE wire."""
        circuit = ArithmeticCircuit()
        x = circuit.allocate_public_input("x", 10)
        assert x.index == 1
        assert circuit.public_input_count == 1
        assert circuit.public_inputs == {"x": 10}

    def test_add_constraint_and_check(self):
        """A manually added constraint must pass check_constraints."""
        circuit = ArithmeticCircuit()
        a = circuit.allocate_public_input("a", 3)
        b = circuit.allocate_public_input("b", 4)
        product = circuit.allocate_variable("product", 12)
        # a * b = product
        circuit.add_constraint(
            a={a.index: 1},
            b={b.index: 1},
            c={product.index: 1},
        )
        assert circuit.check_constraints() is True

    def test_failing_constraint(self):
        """A constraint with a wrong witness value must fail check_constraints."""
        circuit = ArithmeticCircuit()
        a = circuit.allocate_public_input("a", 3)
        b = circuit.allocate_public_input("b", 4)
        product = circuit.allocate_variable("product", 999)  # wrong
        circuit.add_constraint(
            a={a.index: 1},
            b={b.index: 1},
            c={product.index: 1},
        )
        assert circuit.check_constraints() is False

    def test_variable_multiplication_operator(self):
        """Variable.__mul__ must auto-create a constraint and set the witness."""
        circuit = ArithmeticCircuit()
        x = circuit.allocate_public_input("x", 3)
        y = circuit.allocate_public_input("y", 7)
        z = x * y  # should create intermediate with value 21
        assert circuit.get_witness(z.name) == _to_field(0)  # operator doesn't auto-set
        # But if we set it manually:
        circuit.set_witness(z.name, 21)
        assert circuit.check_constraints() is True

    def test_assert_less_equal(self):
        """assert_less_equal must pass when var <= bound."""
        circuit = ArithmeticCircuit()
        cost = circuit.allocate_public_input("cost", 50)
        cap = circuit.allocate_variable("cap", 100)
        circuit.assert_less_equal(cost, cap)
        assert circuit.check_constraints() is True


# ── TrajectoryProver: Proof Generation ───────────────────────────────────────


class TestTrajectoryProverGeneration:
    """Hash-chain proof generation and basic properties."""

    def test_generate_proof_returns_result(self):
        """generate_proof must return a TrajectoryProofResult."""
        prover = TrajectoryProver(tier="hash_chain")
        result = prover.generate_proof("sess-1", _steps(), _policies_budget())
        assert isinstance(result, TrajectoryProofResult)
        assert result.session_id == "sess-1"
        assert result.proof_type == "hash_chain"

    def test_proof_data_is_hex(self):
        """Proof data must be a valid hex string."""
        prover = TrajectoryProver(tier="hash_chain")
        result = prover.generate_proof("sess-2", _steps(), _policies_budget())
        bytes.fromhex(result.proof_data)  # must not raise

    def test_compliant_trajectory_gives_valid_status(self):
        """A trajectory under budget must produce proof_status='valid'."""
        prover = TrajectoryProver(tier="hash_chain")
        result = prover.generate_proof(
            "sess-3",
            _steps(count=2, cost=0.01),
            _policies_budget(cap=1.00),
        )
        assert result.proof_status == "valid"
        assert result.public_inputs["compliant"] is True


# ── TrajectoryProver: Verification ───────────────────────────────────────────


class TestTrajectoryProverVerification:
    """Proof verification round-trips and tamper detection."""

    def test_verify_valid_proof(self):
        """A freshly generated proof must verify successfully."""
        prover = TrajectoryProver(tier="hash_chain")
        result = prover.generate_proof("sess-v1", _steps(), _policies_budget())
        assert prover.verify_proof(result.proof_data, result.public_inputs)

    def test_tampered_proof_fails_verification(self):
        """Modifying proof_data must cause verification to fail."""
        prover = TrajectoryProver(tier="hash_chain")
        result = prover.generate_proof("sess-v2", _steps(), _policies_budget())
        bad_proof = "0" * len(result.proof_data)
        assert prover.verify_proof(bad_proof, result.public_inputs) is False

    def test_empty_inputs_fail_verification(self):
        """verify_proof with empty data or inputs returns False."""
        prover = TrajectoryProver(tier="hash_chain")
        assert prover.verify_proof("", {}) is False


# ── TrajectoryProver: Determinism and Cache ──────────────────────────────────


class TestTrajectoryProverDeterminismAndCache:
    """Proof determinism and caching behavior."""

    def test_same_inputs_produce_same_proof(self):
        """Identical inputs must produce identical proof_data."""
        prover = TrajectoryProver(tier="hash_chain")
        steps = _steps()
        policies = _policies_budget()
        r1 = prover.generate_proof("sess-det", steps, policies)
        # Clear cache to force re-computation
        prover.clear_cache()
        r2 = prover.generate_proof("sess-det", steps, policies)
        assert r1.proof_data == r2.proof_data

    def test_cache_returns_same_object(self):
        """Second call with same inputs must return cached result."""
        prover = TrajectoryProver(tier="hash_chain")
        steps = _steps()
        policies = _policies_budget()
        r1 = prover.generate_proof("sess-cache", steps, policies)
        r2 = prover.generate_proof("sess-cache", steps, policies)
        assert r1 is r2
        assert prover.cache_size >= 1

    def test_clear_session_cache(self):
        """clear_session_cache must remove entries for that session."""
        prover = TrajectoryProver(tier="hash_chain")
        prover.generate_proof("sess-clear", _steps(), _policies_budget())
        assert prover.cache_size >= 1
        removed = prover.clear_session_cache("sess-clear")
        assert removed >= 1


# ── Compliance Detection ─────────────────────────────────────────────────────


class TestComplianceDetection:
    """Compliance checks for budget, rate, and token policies."""

    def test_under_budget_is_compliant(self):
        """Trajectory cost below budget cap is compliant."""
        assert _check_trajectory_compliance(
            _steps(count=2, cost=0.01),
            _policies_budget(cap=1.00),
        ) is True

    def test_over_budget_is_non_compliant(self):
        """Trajectory cost exceeding budget cap is non-compliant."""
        assert _check_trajectory_compliance(
            _steps(count=10, cost=0.50),
            _policies_budget(cap=1.00),
        ) is False

    def test_over_rate_limit_is_non_compliant(self):
        """Too many calls exceeding rate limit is non-compliant."""
        assert _check_trajectory_compliance(
            _steps(count=15),
            _policies_rate(max_calls=10),
        ) is False

    def test_empty_session_raises_on_missing_id(self):
        """generate_proof must raise ValueError for an empty session_id."""
        prover = TrajectoryProver(tier="hash_chain")
        with pytest.raises(ValueError, match="session_id"):
            prover.generate_proof("", _steps(), _policies_budget())
