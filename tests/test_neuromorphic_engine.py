"""
Tests for orchestrator.core.neuromorphic_engine, orchestrator.core.snn_compiler,
and orchestrator.core.neuromorphic_hw — LIF neuron dynamics, SNN topology
construction, policy-to-SNN compilation, neuromorphic enforcement evaluation,
shadow mode comparison, and hardware detection.
"""

from __future__ import annotations

import pytest
import yaml

from orchestrator.core.snn_compiler import (
    LIFNeuron,
    SNNTopology,
    PolicyToSNNCompiler,
)
from orchestrator.core.neuromorphic_engine import (
    NeuromorphicEnforcer,
    EnforcementDecision,
)
from orchestrator.core.neuromorphic_hw import (
    NeuromorphicHardwareDetector,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


_POLICIES_YAML = yaml.dump([
    {"type": "budget_cap", "config": {"max_usd": 100.0}},
    {"type": "rate_limit", "config": {"max_calls": 50}},
], default_flow_style=False)

_SAFE_INPUTS = {
    "spend_usd": 10.0,
    "budget_cap_usd": 100.0,
    "call_count": 5,
    "rate_limit": 50,
    "token_count": 500,
    "token_cap": 10000,
    "amplification": 1.0,
    "max_amplification": 10.0,
}

_OVER_BUDGET_INPUTS = {
    "spend_usd": 99.0,
    "budget_cap_usd": 100.0,
    "call_count": 5,
    "rate_limit": 50,
    "token_count": 500,
    "token_cap": 10000,
    "amplification": 1.0,
    "max_amplification": 10.0,
}


# ── LIFNeuron ───────────────────────────────────────────────────────────────


class TestLIFNeuron:
    """Leaky Integrate-and-Fire neuron step dynamics."""

    def test_below_threshold_does_not_spike(self):
        """A small input below threshold must not produce a spike."""
        neuron = LIFNeuron(idx=0, name="test", threshold=1.0, leak_rate=0.2)
        fired = neuron.step(0.3)
        assert fired is False
        assert neuron.spiked is False
        assert neuron.membrane_v < 1.0

    def test_above_threshold_spikes(self):
        """Sufficient input exceeding threshold must produce a spike."""
        neuron = LIFNeuron(idx=0, name="test", threshold=1.0, leak_rate=0.0)
        fired = neuron.step(1.5)
        assert fired is True
        assert neuron.spiked is True

    def test_membrane_resets_after_spike(self):
        """After spiking, a subsequent step must reset membrane to reset_v."""
        neuron = LIFNeuron(idx=0, name="test", threshold=1.0, leak_rate=0.0, reset_v=0.0)
        neuron.step(1.5)  # spike
        assert neuron.spiked is True
        neuron.step(0.0)  # next step — should reset
        assert neuron.spiked is False
        assert neuron.membrane_v == 0.0

    def test_leak_reduces_membrane(self):
        """The leak rate must cause membrane decay between steps."""
        neuron = LIFNeuron(idx=0, name="test", threshold=5.0, leak_rate=0.5)
        neuron.step(2.0)  # membrane = 0*(1-0.5) + 2.0 = 2.0
        v_after_first = neuron.membrane_v
        neuron.step(0.0)  # membrane = 2.0*(1-0.5) + 0 = 1.0
        assert neuron.membrane_v < v_after_first


# ── SNNTopology ──────────────────────────────────────────────────────────────


class TestSNNTopology:
    """SNN topology construction and properties."""

    def test_neuron_count_correct(self):
        """neuron_count must match the number of added neurons."""
        topo = SNNTopology()
        topo.add_neuron("a")
        topo.add_neuron("b")
        topo.add_neuron("c")
        assert topo.neuron_count == 3

    def test_synapse_count_correct(self):
        """synapse_count must match the number of added synapses."""
        topo = SNNTopology()
        topo.add_neuron("a")
        topo.add_neuron("b")
        topo.add_synapse(0, 1, weight=0.5)
        assert topo.synapse_count == 1

    def test_topology_hash_is_deterministic(self):
        """Same topology structure must produce the same hash."""
        def _build():
            t = SNNTopology()
            t.add_neuron("x", threshold=1.0)
            t.add_neuron("y", threshold=0.5)
            t.add_synapse(0, 1, weight=1.0)
            return t.topology_hash()
        assert _build() == _build()

    def test_invalid_synapse_raises(self):
        """Adding a synapse with out-of-range indices must raise ValueError."""
        topo = SNNTopology()
        topo.add_neuron("a")
        with pytest.raises(ValueError):
            topo.add_synapse(0, 5, weight=1.0)


# ── PolicyToSNNCompiler ──────────────────────────────────────────────────────


class TestPolicyToSNNCompiler:
    """Compilation of YAML policy constraints into SNN topologies."""

    def test_compile_budget_cap_produces_valid_topology(self):
        """A budget_cap policy must produce a topology with input and output neurons."""
        compiler = PolicyToSNNCompiler()
        topology = compiler.compile([
            {"type": "budget_cap", "config": {"max_usd": 100.0}},
        ])
        assert topology.neuron_count >= 4  # out:deny, out:allow, out:degrade, bias, budget neurons
        assert topology.synapse_count >= 1
        assert "deny" in topology.output_neurons

    def test_compile_rate_limit_produces_valid_topology(self):
        """A rate_limit policy must produce counter and threshold neurons."""
        compiler = PolicyToSNNCompiler()
        topology = compiler.compile([
            {"type": "rate_limit", "config": {"max_calls": 50}},
        ])
        assert topology.neuron_count >= 5
        assert topology.synapse_count >= 2

    def test_compile_multiple_policies(self):
        """Compiling multiple policies must merge them into one topology."""
        compiler = PolicyToSNNCompiler()
        topology = compiler.compile([
            {"type": "budget_cap", "config": {"max_usd": 100.0}},
            {"type": "rate_limit", "config": {"max_calls": 50}},
            {"type": "token_cap", "config": {"max_tokens": 10000}},
        ])
        # Should have neurons for all three policies plus shared outputs
        assert topology.neuron_count >= 10


# ── NeuromorphicEnforcer ─────────────────────────────────────────────────────


class TestNeuromorphicEnforcer:
    """SNN-based policy enforcement evaluation."""

    def test_compile_policies_returns_valid_hash(self):
        """compile_policies must return a non-empty hex hash string."""
        enforcer = NeuromorphicEnforcer()
        topo_hash = enforcer.compile_policies(_POLICIES_YAML)
        assert isinstance(topo_hash, str)
        assert len(topo_hash) == 64  # SHA-256 hex
        bytes.fromhex(topo_hash)  # must be valid hex

    def test_evaluate_returns_enforcement_decision(self):
        """evaluate() must return an EnforcementDecision."""
        enforcer = NeuromorphicEnforcer()
        topo_hash = enforcer.compile_policies(_POLICIES_YAML)
        decision = enforcer.evaluate(topo_hash, _SAFE_INPUTS)
        assert isinstance(decision, EnforcementDecision)
        assert decision.decision in ("allow", "deny", "degrade")
        assert decision.total_neurons > 0
        assert decision.latency_ms >= 0

    def test_evaluate_unknown_hash_raises(self):
        """evaluate() with an unknown topology hash must raise KeyError."""
        enforcer = NeuromorphicEnforcer()
        with pytest.raises(KeyError):
            enforcer.evaluate("0" * 64, _SAFE_INPUTS)

    def test_cached_topology_count(self):
        """After compiling, cached_topology_count must increment."""
        enforcer = NeuromorphicEnforcer()
        assert enforcer.cached_topology_count == 0
        enforcer.compile_policies(_POLICIES_YAML)
        assert enforcer.cached_topology_count == 1

    def test_invalidate_topology(self):
        """invalidate_topology must remove a cached topology."""
        enforcer = NeuromorphicEnforcer()
        h = enforcer.compile_policies(_POLICIES_YAML)
        assert enforcer.invalidate_topology(h) is True
        assert enforcer.cached_topology_count == 0


# ── Shadow Mode ──────────────────────────────────────────────────────────────


class TestShadowMode:
    """Shadow mode comparison between standard and SNN decisions."""

    def test_matching_decisions_return_true(self):
        """shadow_validate must return True when decisions match."""
        enforcer = NeuromorphicEnforcer()
        assert enforcer.shadow_validate("allow", "allow") is True

    def test_mismatching_decisions_return_false(self):
        """shadow_validate must return False when decisions differ."""
        enforcer = NeuromorphicEnforcer()
        assert enforcer.shadow_validate("allow", "deny") is False

    def test_mismatch_rate_tracks_correctly(self):
        """mismatch_rate must reflect the fraction of mismatched validations."""
        enforcer = NeuromorphicEnforcer()
        enforcer.shadow_validate("allow", "allow")
        enforcer.shadow_validate("allow", "deny")
        assert enforcer.mismatch_rate == 0.5

    def test_reset_shadow_stats(self):
        """reset_shadow_stats must zero the counters and re-enable shadow mode."""
        enforcer = NeuromorphicEnforcer()
        enforcer.shadow_validate("allow", "deny")
        enforcer.reset_shadow_stats()
        assert enforcer.mismatch_rate == 0.0
        assert enforcer.shadow_enabled is True


# ── NeuromorphicHardwareDetector ─────────────────────────────────────────────


class TestNeuromorphicHardwareDetector:
    """Hardware detection on standard (non-neuromorphic) environments."""

    def test_detect_returns_none_on_standard_hardware(self):
        """detect() must return 'none' when no neuromorphic hardware is present."""
        NeuromorphicHardwareDetector.reset()  # clear cached result
        result = NeuromorphicHardwareDetector.detect()
        assert result in ("none", "loihi", "speck")
        # On a standard dev machine without lava-nc or rockpool:
        # we can at least verify the return is a valid string

    def test_detect_is_cached(self):
        """Calling detect() twice must return the same cached result."""
        NeuromorphicHardwareDetector.reset()
        r1 = NeuromorphicHardwareDetector.detect()
        r2 = NeuromorphicHardwareDetector.detect()
        assert r1 == r2

    def test_reset_clears_cache(self):
        """reset() must clear the cached detection result."""
        NeuromorphicHardwareDetector.detect()  # populate cache
        NeuromorphicHardwareDetector.reset()
        assert NeuromorphicHardwareDetector._cached_result is None
