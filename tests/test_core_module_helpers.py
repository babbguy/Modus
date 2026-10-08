"""
Core Modules (conductor_push, ebpf_engine, pqc_assessment,
                                   zk_trajectory_prover, policy_engine helpers)

Tests:
  - conductor_push: circuit breaker, instance ID, push disabled
  - ebpf_engine: SoftwareInterceptor budget checks, events, stats; EBPFEngine lifecycle
  - pqc_assessment: pure functions (detect_classical_crypto, compute_readiness_score,
                    estimate_quantum_break_year, assess_hndl_risk)
  - zk_trajectory_prover: hash-chain prover, verification, cache, TrajectoryProver
  - policy_engine: _window_key, _conditions_match, PolicyResult
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# ═════════════════════════════════════════════════════════════════════════════
# 1. CONDUCTOR PUSH — Unit-level (no DB needed)
# ═════════════════════════════════════════════════════════════════════════════

def test_conductor_get_instance_id_from_settings():
    import orchestrator.core.conductor_push as cp
    with patch.object(cp.settings, "conductor_instance_id", "preset-id"):
        assert cp._get_instance_id() == "preset-id"


def test_conductor_get_instance_id_generated():
    import orchestrator.core.conductor_push as cp
    with patch.object(cp.settings, "conductor_instance_id", ""), \
         patch.object(cp.settings, "app_name", "testapp"), \
         patch.object(cp.settings, "database_url", "sqlite:///:memory:"):
        result = cp._get_instance_id()
        assert len(result) == 16
        # Deterministic
        assert result == cp._get_instance_id()


def test_conductor_get_instance_name():
    import orchestrator.core.conductor_push as cp
    with patch.object(cp.settings, "conductor_instance_name", "My Orch"):
        assert cp._get_instance_name() == "My Orch"
    with patch.object(cp.settings, "conductor_instance_name", ""), \
         patch.object(cp.settings, "app_name", "Modus"):
        assert cp._get_instance_name() == "Modus"


@pytest.mark.asyncio
async def test_conductor_register_disabled():
    import orchestrator.core.conductor_push as cp
    with patch.object(cp.settings, "conductor_url", ""):
        result = await cp.register_with_conductor()
        assert result is False


@pytest.mark.asyncio
async def test_conductor_push_disabled():
    import orchestrator.core.conductor_push as cp
    with patch.object(cp.settings, "conductor_url", ""):
        result = await cp.push_to_conductor()
        assert result is False


def test_conductor_circuit_breaker_state():
    import orchestrator.core.conductor_push as cp
    # Reset module-level state
    cp._consecutive_failures = 0
    cp._circuit_open_until = None
    assert cp._consecutive_failures == 0
    assert cp._circuit_open_until is None


# ═════════════════════════════════════════════════════════════════════════════
# 2. EBPF ENGINE — SoftwareInterceptor
# ═════════════════════════════════════════════════════════════════════════════

def test_software_interceptor_budget():
    from orchestrator.core.ebpf_engine import (
        SoftwareInterceptor, AppBudget, EnforcementAction,
    )
    si = SoftwareInterceptor()
    budget = AppBudget(
        app_id="app1",
        budget_limit_cents=1000,
        spent_cents=0,
        action_on_exceed=EnforcementAction.DROP,
        reset_interval_seconds=0,
    )
    si.set_budget("app1", budget)

    action, reason = si.check_budget("app1", estimated_cost_cents=500)
    assert action == EnforcementAction.ALLOW
    assert "within budget" in reason

    # Exceed budget
    action, reason = si.check_budget("app1", estimated_cost_cents=600)
    assert action == EnforcementAction.DROP
    assert "exceeded" in reason


def test_software_interceptor_no_budget():
    from orchestrator.core.ebpf_engine import SoftwareInterceptor, EnforcementAction
    si = SoftwareInterceptor()
    action, reason = si.check_budget("unknown-app")
    assert action == EnforcementAction.ALLOW
    assert "no budget" in reason


def test_software_interceptor_auto_reset():
    from orchestrator.core.ebpf_engine import (
        SoftwareInterceptor, AppBudget, EnforcementAction,
    )
    si = SoftwareInterceptor()
    budget = AppBudget(
        app_id="app2",
        budget_limit_cents=100,
        spent_cents=99,
        last_reset_epoch=0,  # long ago
        reset_interval_seconds=1,
        action_on_exceed=EnforcementAction.DROP,
    )
    si.set_budget("app2", budget)
    # Should auto-reset because last_reset_epoch is 0 and interval is 1s
    action, reason = si.check_budget("app2", estimated_cost_cents=50)
    assert action == EnforcementAction.ALLOW


def test_software_interceptor_events():
    from orchestrator.core.ebpf_engine import SoftwareInterceptor, InterceptEvent
    si = SoftwareInterceptor()

    event = InterceptEvent(
        timestamp_ns=time.time_ns(),
        pid=1234,
        comm="python3",
        provider="openai",
        destination_ip="104.18.1.1",
        destination_port=443,
        model_hint="gpt-4",
        estimated_tokens=500,
        action_taken="allow",
        latency_us=3.5,
    )
    si.record_event(event)
    events = si.get_recent_events(10)
    assert len(events) == 1
    assert events[0].provider == "openai"


def test_software_interceptor_stats():
    from orchestrator.core.ebpf_engine import SoftwareInterceptor
    si = SoftwareInterceptor()
    stats = si.get_stats()
    assert stats["intercepted"] == 0
    assert stats["avg_latency_us"] == 0.0


def test_software_interceptor_lifecycle():
    from orchestrator.core.ebpf_engine import SoftwareInterceptor
    si = SoftwareInterceptor()
    assert si.is_active is False
    with patch.object(si, "resolve_providers"):
        si.start()
    assert si.is_active is True
    si.stop()
    assert si.is_active is False


def test_software_interceptor_get_budget():
    from orchestrator.core.ebpf_engine import SoftwareInterceptor, AppBudget
    si = SoftwareInterceptor()
    assert si.get_budget("nope") is None
    budget = AppBudget(app_id="x", budget_limit_cents=500)
    si.set_budget("x", budget)
    assert si.get_budget("x") is not None


# ═════════════════════════════════════════════════════════════════════════════
# 3. EBPF ENGINE — EBPFEngine (always software fallback on Windows/test)
# ═════════════════════════════════════════════════════════════════════════════

def test_ebpf_engine_backend_detection():
    from orchestrator.core.ebpf_engine import EBPFEngine, EBPFBackend
    engine = EBPFEngine()
    # On Windows/test, should be SOFTWARE
    assert engine.backend in (EBPFBackend.SOFTWARE, EBPFBackend.BCC, EBPFBackend.LIBBPF)


def test_ebpf_engine_start_software():
    from orchestrator.core.ebpf_engine import EBPFEngine, EBPFBackend
    engine = EBPFEngine()
    engine._backend = EBPFBackend.SOFTWARE
    with patch.object(engine._software, "resolve_providers"):
        result = engine.start()
    assert result is False  # software fallback returns False
    assert engine.is_active is True
    engine.stop()
    assert engine.is_active is False


def test_ebpf_engine_stats_software():
    from orchestrator.core.ebpf_engine import EBPFEngine, EBPFBackend
    engine = EBPFEngine()
    engine._backend = EBPFBackend.SOFTWARE
    with patch.object(engine._software, "resolve_providers"):
        engine.start()
    stats = engine.get_stats()
    assert stats.backend == "software"
    assert stats.bpf_program_loaded is False
    engine.stop()


def test_ebpf_engine_set_get_budget_software():
    from orchestrator.core.ebpf_engine import EBPFEngine, EBPFBackend, AppBudget
    engine = EBPFEngine()
    engine._backend = EBPFBackend.SOFTWARE
    budget = AppBudget(app_id="test", budget_limit_cents=5000)
    engine.set_budget("test", budget)
    retrieved = engine.get_budget("test")
    assert retrieved is not None
    assert retrieved.budget_limit_cents == 5000


def test_ebpf_engine_events_software():
    from orchestrator.core.ebpf_engine import EBPFEngine, EBPFBackend
    engine = EBPFEngine()
    engine._backend = EBPFBackend.SOFTWARE
    events = engine.get_recent_events(10)
    assert events == []


def test_ebpf_engine_already_started():
    from orchestrator.core.ebpf_engine import EBPFEngine, EBPFBackend
    engine = EBPFEngine()
    engine._backend = EBPFBackend.SOFTWARE
    engine._active = True
    result = engine.start()
    assert result is True  # returns True when already active


# ═════════════════════════════════════════════════════════════════════════════
# 4. PQC ASSESSMENT — Pure Functions
# ═════════════════════════════════════════════════════════════════════════════

def test_detect_classical_crypto():
    from orchestrator.core.pqc_assessment import detect_classical_crypto
    metadata = {"algorithm": "RSA-2048", "signature": "ECDSA-P256"}
    detected = detect_classical_crypto(metadata)
    algo_names = [d["algorithm"] for d in detected]
    assert "RSA-2048" in algo_names
    assert "ECDSA-P256" in algo_names


def test_detect_classical_crypto_empty():
    from orchestrator.core.pqc_assessment import detect_classical_crypto
    assert detect_classical_crypto({}) == []
    assert detect_classical_crypto(None) == []


def test_detect_classical_crypto_no_match():
    from orchestrator.core.pqc_assessment import detect_classical_crypto
    assert detect_classical_crypto({"algorithm": "ML-KEM-768"}) == []


def test_compute_readiness_score_all_pqc():
    from orchestrator.core.pqc_assessment import compute_readiness_score
    score = compute_readiness_score(0, 0, 10, None)
    assert score == 100.0


def test_compute_readiness_score_all_classical():
    from orchestrator.core.pqc_assessment import compute_readiness_score
    score = compute_readiness_score(10, 0, 0, "3DES")
    assert score < 10  # very low with near-term break


def test_compute_readiness_score_mixed():
    from orchestrator.core.pqc_assessment import compute_readiness_score
    score = compute_readiness_score(5, 3, 2, None)
    # (2*100 + 3*50) / 10 = 35
    assert score == 35.0


def test_compute_readiness_score_no_assets():
    from orchestrator.core.pqc_assessment import compute_readiness_score
    assert compute_readiness_score(0, 0, 0, None) == 100.0


def test_estimate_quantum_break_year_known():
    from orchestrator.core.pqc_assessment import estimate_quantum_break_year
    assert estimate_quantum_break_year("RSA-2048", 2048) == 2030
    assert estimate_quantum_break_year("AES-256", 256) == 2060


def test_estimate_quantum_break_year_pqc():
    from orchestrator.core.pqc_assessment import estimate_quantum_break_year
    assert estimate_quantum_break_year("ML-KEM-768", 768) == 2100


def test_estimate_quantum_break_year_unknown():
    from orchestrator.core.pqc_assessment import estimate_quantum_break_year
    year = estimate_quantum_break_year("UnknownAlgo", 128)
    assert year > 2025


def test_assess_hndl_risk_critical():
    from orchestrator.core.pqc_assessment import assess_hndl_risk
    result = assess_hndl_risk("3DES", 168, "top_secret", 10)
    assert result["risk_level"] == "critical"
    assert result["enforcement_action"] == "blocked"


def test_assess_hndl_risk_safe():
    from orchestrator.core.pqc_assessment import assess_hndl_risk
    result = assess_hndl_risk("AES-256", 256, "standard", 5)
    assert result["risk_level"] == "safe"
    assert result["enforcement_action"] == "allowed"


def test_assess_hndl_risk_monitor():
    from orchestrator.core.pqc_assessment import assess_hndl_risk
    result = assess_hndl_risk("RSA-4096", 4096, "standard", 3)
    assert result["risk_level"] in ("monitor", "safe")


def test_assess_hndl_risk_pqc_safe():
    from orchestrator.core.pqc_assessment import assess_hndl_risk
    result = assess_hndl_risk("ML-KEM-768", 768, "top_secret", 10)
    assert result["risk_level"] == "safe"


def test_get_replacement_known():
    from orchestrator.core.pqc_assessment import _get_replacement
    assert _get_replacement("RSA-2048") == "ML-KEM-768"


def test_get_replacement_unknown():
    from orchestrator.core.pqc_assessment import _get_replacement
    assert "ML-KEM-768" in _get_replacement("UnknownAlgo")


# ═════════════════════════════════════════════════════════════════════════════
# 5. ZK TRAJECTORY PROVER
# ═════════════════════════════════════════════════════════════════════════════

def test_hash_chain_basic():
    from orchestrator.core.zk_trajectory_prover import _prove_hash_chain
    steps = [
        {"call_idx": 0, "model": "gpt-4", "cost_usd": 0.05, "tokens": 500},
        {"call_idx": 1, "model": "gpt-4", "cost_usd": 0.03, "tokens": 300},
    ]
    policies = [
        {"type": "budget_cap", "config": {"cap_usd": 1.00}},
    ]
    result = _prove_hash_chain("session-1", steps, policies)
    assert result.proof_status == "valid"
    assert result.proof_type == "hash_chain"
    assert len(result.proof_data) == 64  # SHA-256 hex


def test_hash_chain_violation():
    from orchestrator.core.zk_trajectory_prover import _prove_hash_chain
    steps = [
        {"call_idx": 0, "model": "gpt-4", "cost_usd": 2.00, "tokens": 500},
    ]
    policies = [
        {"type": "budget_cap", "config": {"cap_usd": 1.00}},
    ]
    result = _prove_hash_chain("session-2", steps, policies)
    assert result.proof_status == "invalid"


def test_hash_chain_empty_steps():
    from orchestrator.core.zk_trajectory_prover import _prove_hash_chain
    result = _prove_hash_chain("session-3", [], [])
    assert result.proof_status == "valid"


def test_verify_hash_chain():
    from orchestrator.core.zk_trajectory_prover import _prove_hash_chain, _verify_hash_chain
    steps = [{"call_idx": 0, "model": "gpt-4", "cost_usd": 0.01, "tokens": 100}]
    policies = [{"type": "budget_cap", "config": {"cap_usd": 10.00}}]
    result = _prove_hash_chain("session-v", steps, policies)
    verified = _verify_hash_chain(result.proof_data, result.public_inputs)
    assert verified is True


def test_verify_hash_chain_tampered():
    from orchestrator.core.zk_trajectory_prover import _verify_hash_chain
    assert _verify_hash_chain("deadbeef" * 8, {
        "policy_digest": "a" * 64,
        "step_digest": "b" * 64,
        "outcome_digest": "c" * 64,
    }) is False


def test_verify_hash_chain_missing_inputs():
    from orchestrator.core.zk_trajectory_prover import _verify_hash_chain
    assert _verify_hash_chain("abcd", {}) is False


def test_trajectory_compliance_check():
    from orchestrator.core.zk_trajectory_prover import _check_trajectory_compliance
    steps = [{"cost_usd": 0.5, "tokens": 100}]
    policies = [{"type": "budget_cap", "config": {"cap_usd": 1.0}}]
    assert _check_trajectory_compliance(steps, policies) is True

    policies_strict = [{"type": "budget_cap", "config": {"cap_usd": 0.1}}]
    assert _check_trajectory_compliance(steps, policies_strict) is False


def test_trajectory_compliance_rate_limit():
    from orchestrator.core.zk_trajectory_prover import _check_trajectory_compliance
    steps = [{"cost_usd": 0} for _ in range(10)]
    policies = [{"type": "rate_limit", "config": {"max_calls": 5}}]
    assert _check_trajectory_compliance(steps, policies) is False


def test_trajectory_compliance_token_cap():
    from orchestrator.core.zk_trajectory_prover import _check_trajectory_compliance
    steps = [{"cost_usd": 0, "tokens": 600}]
    policies = [{"type": "token_cap", "config": {"max_tokens": 500}}]
    assert _check_trajectory_compliance(steps, policies) is False


# ═════════════════════════════════════════════════════════════════════════════
# 6. ZK — PROOF CACHE
# ═════════════════════════════════════════════════════════════════════════════

def test_proof_cache():
    from orchestrator.core.zk_trajectory_prover import _ProofCache, TrajectoryProofResult
    cache = _ProofCache(max_size=5)
    assert cache.size == 0

    result = TrajectoryProofResult(
        session_id="s1", proof_type="hash_chain", proof_status="valid",
        proof_data="abc", public_inputs={}, circuit_size=0,
        prover_time_ms=1, created_at="2026-01-01T00:00:00Z",
    )
    cache.put("s1", ["ph1"], "th1", result)
    assert cache.size == 1

    cached = cache.get("s1", ["ph1"], "th1")
    assert cached is result

    assert cache.get("s2", ["ph1"], "th1") is None

    removed = cache.clear_session("s1")
    assert removed == 1
    assert cache.size == 0


def test_proof_cache_eviction():
    from orchestrator.core.zk_trajectory_prover import _ProofCache, TrajectoryProofResult
    cache = _ProofCache(max_size=2)
    for i in range(3):
        result = TrajectoryProofResult(
            session_id=f"s{i}", proof_type="hash_chain", proof_status="valid",
            proof_data="x", public_inputs={}, circuit_size=0,
            prover_time_ms=1, created_at="2026-01-01T00:00:00Z",
        )
        cache.put(f"s{i}", [f"p{i}"], f"t{i}", result)
    assert cache.size == 2


def test_proof_cache_clear():
    from orchestrator.core.zk_trajectory_prover import _ProofCache, TrajectoryProofResult
    cache = _ProofCache(max_size=10)
    result = TrajectoryProofResult(
        session_id="s", proof_type="hash_chain", proof_status="valid",
        proof_data="x", public_inputs={}, circuit_size=0,
        prover_time_ms=1, created_at="now",
    )
    cache.put("s", ["p"], "t", result)
    cache.clear()
    assert cache.size == 0


# ═════════════════════════════════════════════════════════════════════════════
# 7. ZK — TrajectoryProver class
# ═════════════════════════════════════════════════════════════════════════════

def test_trajectory_prover_hash_chain():
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver
    prover = TrajectoryProver(tier="hash_chain")
    assert prover.tier == "hash_chain"

    steps = [{"call_idx": 0, "model": "gpt-4", "cost_usd": 0.05, "tokens": 500}]
    policies = [{"type": "budget_cap", "config": {"cap_usd": 1.00}}]
    result = prover.generate_proof("session-1", steps, policies)
    assert result.proof_status == "valid"

    # Verify
    assert prover.verify_proof(result.proof_data, result.public_inputs) is True

    # Cache hit
    result2 = prover.generate_proof("session-1", steps, policies)
    assert result2 is result  # same object from cache
    assert prover.cache_size == 1


def test_trajectory_prover_empty_proof():
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver
    prover = TrajectoryProver(tier="hash_chain")
    assert prover.verify_proof("", {}) is False
    assert prover.verify_proof("abc", None) is False


def test_trajectory_prover_invalid_tier():
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver
    with pytest.raises(ValueError):
        TrajectoryProver(tier="invalid-tier")


def test_trajectory_prover_empty_session_id():
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver
    prover = TrajectoryProver(tier="hash_chain")
    with pytest.raises(ValueError):
        prover.generate_proof("", [], [])


def test_trajectory_prover_clear_cache():
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver
    prover = TrajectoryProver(tier="hash_chain")
    steps = [{"call_idx": 0, "model": "m", "cost_usd": 0, "tokens": 0}]
    prover.generate_proof("s1", steps, [])
    assert prover.cache_size == 1
    prover.clear_session_cache("s1")
    assert prover.cache_size == 0
    prover.generate_proof("s2", steps, [])
    prover.clear_cache()
    assert prover.cache_size == 0


def test_trajectory_prover_repr():
    from orchestrator.core.zk_trajectory_prover import TrajectoryProver
    prover = TrajectoryProver(tier="hash_chain")
    assert "hash_chain" in repr(prover)


# ═════════════════════════════════════════════════════════════════════════════
# 8. POLICY ENGINE — Helpers
# ═════════════════════════════════════════════════════════════════════════════

def test_window_key_hourly():
    from orchestrator.core.policy_engine import _window_key
    now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
    key, start, end = _window_key("hourly", now)
    assert key == "hourly:2026-04-09T14"
    assert start.minute == 0
    assert end.hour == 15


def test_window_key_daily():
    from orchestrator.core.policy_engine import _window_key
    now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
    key, start, end = _window_key("daily", now)
    assert key == "daily:2026-04-09"
    assert start.hour == 0


def test_window_key_monthly():
    from orchestrator.core.policy_engine import _window_key
    now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
    key, start, end = _window_key("monthly", now)
    assert key == "monthly:2026-04"
    assert start.day == 1
    assert end.month == 5


def test_window_key_monthly_december():
    from orchestrator.core.policy_engine import _window_key
    now = datetime(2026, 12, 15, 0, 0, 0, tzinfo=timezone.utc)
    key, start, end = _window_key("monthly", now)
    assert key == "monthly:2026-12"
    assert end.year == 2027
    assert end.month == 1


def test_window_key_invalid():
    from orchestrator.core.policy_engine import _window_key
    now = datetime(2026, 4, 9, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        _window_key("weekly", now)


def test_conditions_match_empty():
    from orchestrator.core.policy_engine import _conditions_match, EvaluateRequest
    req = EvaluateRequest(app_id="a", team_id="t", provider="openai")
    assert _conditions_match(None, req) is True
    assert _conditions_match({}, req) is True


def test_conditions_match_provider():
    from orchestrator.core.policy_engine import _conditions_match, EvaluateRequest
    req = EvaluateRequest(app_id="a", team_id="t", provider="openai")
    assert _conditions_match({"providers": ["openai"]}, req) is True
    assert _conditions_match({"providers": ["anthropic"]}, req) is False


def test_conditions_match_model_pattern():
    from orchestrator.core.policy_engine import _conditions_match, EvaluateRequest
    req = EvaluateRequest(app_id="a", team_id="t", provider="openai", model="gpt-4o-mini")
    assert _conditions_match({"model_pattern": "gpt-4*"}, req) is True
    assert _conditions_match({"model_pattern": "claude-*"}, req) is False


def test_conditions_match_model_pattern_no_model():
    from orchestrator.core.policy_engine import _conditions_match, EvaluateRequest
    req = EvaluateRequest(app_id="a", team_id="t", provider="openai")
    assert _conditions_match({"model_pattern": "gpt-*"}, req) is False


def test_conditions_match_environment():
    from orchestrator.core.policy_engine import _conditions_match, EvaluateRequest
    req = EvaluateRequest(app_id="a", team_id="t", provider="openai", environment="staging")
    assert _conditions_match({"environments": ["staging", "dev"]}, req) is True
    assert _conditions_match({"environments": ["production"]}, req) is False


def test_conditions_match_resource_types():
    from orchestrator.core.policy_engine import _conditions_match, EvaluateRequest
    req = EvaluateRequest(app_id="a", team_id="t", provider="openai", resource_type="embedding")
    assert _conditions_match({"resource_types": ["embedding"]}, req) is True
    assert _conditions_match({"resource_types": ["llm_call"]}, req) is False


def test_conditions_match_default_resource_type():
    from orchestrator.core.policy_engine import _conditions_match, EvaluateRequest
    req = EvaluateRequest(app_id="a", team_id="t", provider="openai")
    assert _conditions_match({"resource_types": ["llm_call"]}, req) is True


def test_policy_result_allowed():
    from orchestrator.core.policy_engine import PolicyResult
    allow = PolicyResult(decision="allow", reason="ok")
    assert allow.allowed is True
    deny = PolicyResult(decision="deny", reason="blocked")
    assert deny.allowed is False
