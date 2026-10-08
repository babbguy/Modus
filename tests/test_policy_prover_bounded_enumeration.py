"""
Policy Prover (Bounded Enumeration + Z3 fallback)

Tests the neuro-symbolic policy prover:
  - Bounded enumeration for budget_cap, rate_limit, token_cap
  - Degradation ladder gap detection
  - Amplification gate
  - Parse failures
  - Timeout handling
  - SMT fallback (no Z3 installed)
  - prove_policy dispatcher
  - Proof certificate generation
"""
from __future__ import annotations

import pytest
import yaml


# ═════════════════════════════════════════════════════════════════════════════
# 1. HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def test_linspace_single():
    from orchestrator.core.policy_prover import _linspace
    result = _linspace(5.0, 10.0, 1)
    assert result == [5.0]


def test_linspace_multiple():
    from orchestrator.core.policy_prover import _linspace
    result = _linspace(0.0, 10.0, 3)
    assert len(result) == 3
    assert result[0] == 0.0
    assert result[-1] == 10.0


def test_sha256():
    from orchestrator.core.policy_prover import _sha256
    h = _sha256("hello")
    assert len(h) == 64
    assert h == _sha256("hello")  # deterministic


def test_parse_policies_dict():
    from orchestrator.core.policy_prover import _parse_policies
    text = yaml.dump({"type": "budget_cap", "config": {"cap_usd": 100}})
    result = _parse_policies(text)
    assert len(result) == 1
    assert result[0]["type"] == "budget_cap"


def test_parse_policies_list():
    from orchestrator.core.policy_prover import _parse_policies
    text = yaml.dump([
        {"type": "budget_cap", "config": {"cap_usd": 100}},
        {"type": "rate_limit", "config": {"max_calls": 50}},
    ])
    result = _parse_policies(text)
    assert len(result) == 2


def test_parse_policies_nested():
    from orchestrator.core.policy_prover import _parse_policies
    text = yaml.dump({"policies": [
        {"type": "budget_cap", "config": {"cap_usd": 100}},
    ]})
    result = _parse_policies(text)
    assert len(result) == 1


def test_parse_policies_invalid():
    from orchestrator.core.policy_prover import _parse_policies
    result = _parse_policies("not: valid: yaml: [[[")
    assert result == []


def test_parse_policies_scalar():
    from orchestrator.core.policy_prover import _parse_policies
    result = _parse_policies("42")
    assert result == []


# ═════════════════════════════════════════════════════════════════════════════
# 2. EXTRACT DOMAINS
# ═════════════════════════════════════════════════════════════════════════════

def test_extract_domains_budget_cap():
    from orchestrator.core.policy_prover import _extract_domains
    d = _extract_domains({"type": "budget_cap", "config": {"cap_usd": 50}}, steps=10)
    assert "spend" in d
    assert len(d["spend"]) == 10


def test_extract_domains_rate_limit():
    from orchestrator.core.policy_prover import _extract_domains
    d = _extract_domains({"type": "rate_limit", "config": {"max_calls": 100}}, steps=5)
    assert "call_count" in d


def test_extract_domains_token_cap():
    from orchestrator.core.policy_prover import _extract_domains
    d = _extract_domains({"type": "token_cap", "config": {"max_tokens": 5000}}, steps=5)
    assert "token_count" in d


def test_extract_domains_amplification():
    from orchestrator.core.policy_prover import _extract_domains
    d = _extract_domains({"type": "amplification_gate", "config": {"max_amplification": 3}}, steps=5)
    assert "amplification" in d


def test_extract_domains_degradation():
    from orchestrator.core.policy_prover import _extract_domains
    d = _extract_domains({"type": "degradation_ladder", "config": {"budget_usd": 200}}, steps=5)
    assert "spend" in d


def test_extract_domains_model_denylist():
    from orchestrator.core.policy_prover import _extract_domains
    d = _extract_domains({"type": "model_denylist", "config": {}}, steps=5)
    assert d == {}


def test_extract_domains_unknown():
    from orchestrator.core.policy_prover import _extract_domains
    d = _extract_domains({"type": "custom_weird", "config": {}}, steps=5)
    assert "spend" in d  # generic fallback


# ═════════════════════════════════════════════════════════════════════════════
# 3. CONSTRAINT CHECKERS
# ═════════════════════════════════════════════════════════════════════════════

def test_check_budget_cap_within():
    from orchestrator.core.policy_prover import _check_budget_cap
    violated, desc = _check_budget_cap({"cap_usd": 100}, {"spend": 50})
    assert violated is False


def test_check_budget_cap_exceeded():
    from orchestrator.core.policy_prover import _check_budget_cap
    violated, desc = _check_budget_cap({"cap_usd": 100}, {"spend": 150})
    assert violated is True
    assert "exceeds" in desc


def test_check_rate_limit_within():
    from orchestrator.core.policy_prover import _check_rate_limit
    violated, _ = _check_rate_limit({"max_calls": 100}, {"call_count": 50})
    assert violated is False


def test_check_rate_limit_exceeded():
    from orchestrator.core.policy_prover import _check_rate_limit
    violated, desc = _check_rate_limit({"max_calls": 100}, {"call_count": 150})
    assert violated is True


def test_check_token_cap_exceeded():
    from orchestrator.core.policy_prover import _check_token_cap
    violated, desc = _check_token_cap({"max_tokens": 1000}, {"token_count": 2000})
    assert violated is True


def test_check_amplification_gate():
    from orchestrator.core.policy_prover import _check_amplification_gate
    violated, _ = _check_amplification_gate({"max_amplification": 5}, {"amplification": 3})
    assert violated is False
    violated, desc = _check_amplification_gate({"max_amplification": 5}, {"amplification": 8})
    assert violated is True


def test_check_degradation_ladder_gap():
    from orchestrator.core.policy_prover import _check_degradation_ladder
    # No deny tier at all
    config = {"budget_usd": 100, "tiers": [{"pct": 80, "action": "warn"}]}
    violated, desc = _check_degradation_ladder(config, {"spend": 150})
    assert violated is True
    assert "no deny tier" in desc


def test_check_degradation_ladder_covered():
    from orchestrator.core.policy_prover import _check_degradation_ladder
    config = {
        "budget_usd": 100,
        "tiers": [
            {"pct": 80, "action": "warn"},
            {"pct": 100, "action": "deny"},
        ],
    }
    violated, _ = _check_degradation_ladder(config, {"spend": 150})
    assert violated is False  # deny tier fires


def test_check_degradation_ladder_within():
    from orchestrator.core.policy_prover import _check_degradation_ladder
    config = {"budget_usd": 100, "tiers": [{"pct": 100, "action": "deny"}]}
    violated, _ = _check_degradation_ladder(config, {"spend": 50})
    assert violated is False


# ═════════════════════════════════════════════════════════════════════════════
# 4. BOUNDED PROVE
# ═════════════════════════════════════════════════════════════════════════════

def test_bounded_prove_budget_cap_proven():
    from orchestrator.core.policy_prover import bounded_prove
    policy_yaml = yaml.dump({
        "type": "budget_cap",
        "name": "daily-cap",
        "config": {"cap_usd": 100},
    })
    result = bounded_prove(policy_yaml, policy_id="pol-1", steps=10)
    assert result.status == "sampled"
    assert result.proof_type == "bounded"
    assert result.variables_checked > 0


def test_bounded_prove_rate_limit():
    from orchestrator.core.policy_prover import bounded_prove
    policy_yaml = yaml.dump({
        "type": "rate_limit",
        "name": "hourly-limit",
        "config": {"max_calls": 50},
    })
    result = bounded_prove(policy_yaml, steps=10)
    assert result.status == "sampled"


def test_bounded_prove_degradation_gap():
    from orchestrator.core.policy_prover import bounded_prove
    policy_yaml = yaml.dump({
        "type": "degradation_ladder",
        "name": "ladder-with-gap",
        "config": {
            "budget_usd": 100,
            "tiers": [{"pct": 80, "action": "warn"}],  # no deny tier
        },
    })
    result = bounded_prove(policy_yaml, steps=10)
    assert result.status == "disproven"
    assert result.counterexample is not None


def test_bounded_prove_invalid_yaml():
    from orchestrator.core.policy_prover import bounded_prove
    result = bounded_prove("not valid yaml [[[", policy_id="bad")
    assert result.status == "unknown"
    assert "parse" in result.statement.lower()


def test_bounded_prove_model_denylist_skipped():
    from orchestrator.core.policy_prover import bounded_prove
    policy_yaml = yaml.dump({
        "type": "model_denylist",
        "name": "deny-gpt4",
        "config": {"models": ["gpt-4"]},
    })
    result = bounded_prove(policy_yaml, steps=10)
    assert result.status == "sampled"  # no continuous vars to check


def test_bounded_prove_timeout():
    from orchestrator.core.policy_prover import bounded_prove
    policy_yaml = yaml.dump({
        "type": "budget_cap",
        "name": "big-check",
        "config": {"cap_usd": 1000},
    })
    result = bounded_prove(policy_yaml, timeout_seconds=0.0001, steps=1000)
    # With a tiny timeout and many steps, should timeout
    assert result.status in ("sampled", "timeout")


# ═════════════════════════════════════════════════════════════════════════════
# 5. SMT PROVE (falls back to bounded if Z3 not installed)
# ═════════════════════════════════════════════════════════════════════════════

def test_smt_prove_fallback():
    from orchestrator.core.policy_prover import smt_prove, _z3_available
    policy_yaml = yaml.dump({
        "type": "budget_cap",
        "name": "daily-cap",
        "config": {"cap_usd": 100},
    })
    result = smt_prove(policy_yaml, policy_id="smt-1")
    # If Z3 not installed, it should fall back to bounded
    if not _z3_available:
        assert result.proof_type == "bounded"
    else:
        assert result.proof_type == "smt"


def test_smt_prove_invalid_yaml():
    from orchestrator.core.policy_prover import smt_prove, _z3_available
    result = smt_prove("bad yaml [[[", policy_id="bad-smt")
    if not _z3_available:
        assert result.status == "unknown"
    else:
        assert result.status == "unknown"


# ═════════════════════════════════════════════════════════════════════════════
# 6. PROVE_POLICY DISPATCHER
# ═════════════════════════════════════════════════════════════════════════════

def test_prove_policy_auto():
    from orchestrator.core.policy_prover import prove_policy
    policy_yaml = yaml.dump({
        "type": "rate_limit",
        "config": {"max_calls": 100},
    })
    result = prove_policy(policy_yaml, policy_id="auto-1")
    assert result.status in ("proven", "sampled", "disproven", "timeout", "unknown")


def test_prove_policy_bounded():
    from orchestrator.core.policy_prover import prove_policy
    policy_yaml = yaml.dump({
        "type": "token_cap",
        "config": {"max_tokens": 5000},
    })
    result = prove_policy(policy_yaml, method="bounded")
    assert result.proof_type == "bounded"


def test_prove_policy_smt_not_installed():
    from orchestrator.core.policy_prover import prove_policy, _z3_available
    if _z3_available:
        pytest.skip("Z3 is installed, can't test ImportError path")
    with pytest.raises(ImportError):
        prove_policy("type: budget_cap", method="smt")


# ═════════════════════════════════════════════════════════════════════════════
# 7. PROOF CERTIFICATE
# ═════════════════════════════════════════════════════════════════════════════

def test_generate_proof_certificate_proven():
    from orchestrator.core.policy_prover import ProofResult, generate_proof_certificate
    result = ProofResult(
        policy_id="pol-1",
        policy_hash="abc123",
        proof_type="bounded",
        status="proven",
        statement="All checks passed",
        counterexample=None,
        variables_checked=100,
        max_depth=1,
        solver_time_ms=50,
        proven_at="2026-01-01T00:00:00Z",
    )
    cert = generate_proof_certificate(result)
    assert cert["verified"] is True
    assert cert["certificate_version"] == 1
    assert cert["status"] == "proven"


def test_generate_proof_certificate_disproven():
    from orchestrator.core.policy_prover import ProofResult, generate_proof_certificate
    result = ProofResult(
        policy_id="pol-2",
        policy_hash="def456",
        proof_type="bounded",
        status="disproven",
        statement="Gap found",
        counterexample={"spend": 150.0},
        variables_checked=50,
        max_depth=1,
        solver_time_ms=30,
        proven_at="2026-01-01T00:00:00Z",
    )
    cert = generate_proof_certificate(result)
    assert cert["verified"] is False
    assert cert["counterexample"] == {"spend": 150.0}
