"""
Tests for orchestrator.core.policy_prover — bounded enumeration prover,
prove_policy entry point, ProofResult dataclass, and proof certificates.
"""

from __future__ import annotations


from orchestrator.core.policy_prover import (
    ProofResult,
    bounded_prove,
    prove_policy,
    generate_proof_certificate,
)


# ── Sample YAML fixtures ────────────────────────────────────────────────────

SIMPLE_BUDGET_YAML = '''
version: "1"
policies:
  - name: test-budget
    type: budget_cap
    config:
      cap_usd: "100.00"
      period: monthly
'''

RATE_LIMIT_YAML = '''
version: "1"
policies:
  - name: test-rate
    type: rate_limit
    config:
      max_calls: 100
      window_seconds: 60
'''

INVALID_YAML = '''
  - this: is
    broken: [
      yaml
'''

EMPTY_YAML = ''


# ── Bounded Prove ────────────────────────────────────────────────────────────


class TestBoundedProve:
    def test_bounded_prove_simple_budget_cap(self):
        """A simple budget_cap policy should be provable."""
        result = bounded_prove(SIMPLE_BUDGET_YAML, policy_id="budget-1")
        assert isinstance(result, ProofResult)
        assert result.status == "sampled"
        assert result.policy_id == "budget-1"
        assert result.proof_type == "bounded"
        assert result.variables_checked > 0

    def test_bounded_prove_rate_limit(self):
        """A rate_limit policy should be provable."""
        result = bounded_prove(RATE_LIMIT_YAML, policy_id="rate-1")
        assert isinstance(result, ProofResult)
        assert result.status == "sampled"
        assert result.proof_type == "bounded"
        assert result.variables_checked > 0

    def test_bounded_prove_invalid_yaml(self):
        """Invalid YAML should return status 'unknown' gracefully."""
        result = bounded_prove(INVALID_YAML, policy_id="bad-yaml")
        assert isinstance(result, ProofResult)
        assert result.status == "unknown"

    def test_bounded_prove_empty_yaml(self):
        """Empty YAML string should be handled gracefully."""
        result = bounded_prove(EMPTY_YAML, policy_id="empty")
        assert isinstance(result, ProofResult)
        assert result.status in ("unknown", "sampled")

    def test_bounded_prove_timeout(self):
        """Very short timeout with many steps should produce a timeout."""
        # Use a huge step count so enumeration takes a while, with a
        # near-zero timeout to force the timeout path.
        result = bounded_prove(
            SIMPLE_BUDGET_YAML,
            policy_id="timeout-test",
            timeout_seconds=0.0,
            steps=100000,
        )
        assert isinstance(result, ProofResult)
        assert result.status == "timeout"


# ── prove_policy entry point ─────────────────────────────────────────────────


class TestProvePolicy:
    def test_prove_policy_auto_method(self):
        """method='auto' should run without error."""
        result = prove_policy(SIMPLE_BUDGET_YAML, policy_id="auto-1", method="auto")
        assert isinstance(result, ProofResult)
        assert result.status in ("proven", "sampled", "disproven", "timeout", "unknown")

    def test_prove_policy_bounded_method(self):
        """method='bounded' should use the bounded prover."""
        result = prove_policy(RATE_LIMIT_YAML, policy_id="bounded-1", method="bounded")
        assert isinstance(result, ProofResult)
        assert result.proof_type == "bounded"


# ── ProofResult fields ───────────────────────────────────────────────────────


class TestProofResultFields:
    def test_proof_result_fields(self):
        """All expected fields should be populated."""
        result = bounded_prove(SIMPLE_BUDGET_YAML, policy_id="fields-test")
        assert result.policy_id == "fields-test"
        assert isinstance(result.policy_hash, str) and len(result.policy_hash) == 64
        assert result.proof_type in ("bounded", "smt")
        assert result.status in ("proven", "sampled", "disproven", "timeout", "unknown")
        assert isinstance(result.statement, str) and len(result.statement) > 0
        assert isinstance(result.variables_checked, int)
        assert isinstance(result.max_depth, int)
        assert isinstance(result.solver_time_ms, int) and result.solver_time_ms >= 0
        assert isinstance(result.proven_at, str) and len(result.proven_at) > 0


# ── Proof Certificate ────────────────────────────────────────────────────────


class TestProofCertificate:
    def test_generate_proof_certificate(self):
        """Certificate should be a dict with all required keys."""
        result = bounded_prove(SIMPLE_BUDGET_YAML, policy_id="cert-test")
        cert = generate_proof_certificate(result)
        assert isinstance(cert, dict)
        assert cert["policy_id"] == "cert-test"
        assert cert["policy_hash"] == result.policy_hash
        assert cert["status"] == result.status
        assert cert["proof_type"] == result.proof_type
        assert cert["statement"] == result.statement
        assert "certificate_version" in cert
        assert "verified" in cert
        assert cert["verified"] == (result.status == "proven")


# ── Determinism ──────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_proof_hash_deterministic(self):
        """Same YAML input should always produce the same policy_hash."""
        r1 = bounded_prove(SIMPLE_BUDGET_YAML, policy_id="det-1")
        r2 = bounded_prove(SIMPLE_BUDGET_YAML, policy_id="det-2")
        assert r1.policy_hash == r2.policy_hash
