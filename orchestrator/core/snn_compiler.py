"""
Modus — Spiking Neural Network Policy Compiler (Phase 8e)
=============================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Compiles YAML policy constraints into Spiking Neural Network (SNN)
topologies for neuromorphic enforcement.  Each policy type maps to a
small subnetwork of Leaky Integrate-and-Fire (LIF) neurons connected
by weighted synapses.  The resulting topology is a lightweight graph
that can be evaluated by the software emulator (neuromorphic_engine.py)
or deployed to real neuromorphic hardware (neuromorphic_hw.py).

Typical network sizes:
    - Single budget cap:      3 neurons, 2 synapses
    - Rate limit chain:       4 neurons, 3 synapses
    - Degradation ladder:     6-8 neurons, 8-12 synapses (WTA circuit)
    - Realistic policy set:   10-50 neurons total

Complies with the Four Laws:
    - Pure Python, stdlib only — no numpy, no scipy
    - Zero external dependencies
    - Compilation < 5 ms for typical policy sets
    - Thread-safe — no mutable module-level state
    - All computation local — no data leaves customer infrastructure
"""

from __future__ import annotations

import hashlib
import logging
import time

logger = logging.getLogger(__name__)


# ── LIF Neuron ───────────────────────────────────────────────────────────────


class LIFNeuron:
    """Leaky Integrate-and-Fire neuron — the fundamental unit of the SNN.

    Membrane voltage accumulates input current, decays by *leak_rate*
    each timestep, and fires a spike when *threshold* is crossed.
    After spiking, membrane resets to *reset_v*.

    Pure Python, no numpy.  Single neuron step < 1 us.
    """

    __slots__ = ("idx", "name", "membrane_v", "threshold", "leak_rate",
                 "reset_v", "spiked")

    def __init__(
        self,
        idx: int,
        name: str,
        threshold: float = 1.0,
        leak_rate: float = 0.2,
        reset_v: float = 0.0,
    ) -> None:
        self.idx = idx
        self.name = name
        self.membrane_v: float = 0.0
        self.threshold = threshold
        self.leak_rate = leak_rate
        self.reset_v = reset_v
        self.spiked: bool = False

    def step(self, input_current: float, dt: float = 1.0) -> bool:
        """Integrate input, apply leak, check threshold, return spike."""
        if self.spiked:
            self.membrane_v = self.reset_v
            self.spiked = False

        # Leaky integration
        self.membrane_v = (
            self.membrane_v * (1.0 - self.leak_rate * dt) + input_current * dt
        )

        # Threshold check
        if self.membrane_v >= self.threshold:
            self.spiked = True
            return True

        return False

    def reset(self) -> None:
        """Reset neuron to initial state."""
        self.membrane_v = 0.0
        self.spiked = False

    def __repr__(self) -> str:
        return (
            f"LIFNeuron(idx={self.idx}, name={self.name!r}, "
            f"v={self.membrane_v:.3f}, thr={self.threshold})"
        )


# ── Synapse ──────────────────────────────────────────────────────────────────


class Synapse:
    """Weighted connection between two neurons.

    Positive weight = excitatory, negative = inhibitory.
    *delay* is in timesteps (0 = immediate propagation).
    """

    __slots__ = ("pre_idx", "post_idx", "weight", "delay")

    def __init__(
        self,
        pre_idx: int,
        post_idx: int,
        weight: float,
        delay: int = 0,
    ) -> None:
        self.pre_idx = pre_idx
        self.post_idx = post_idx
        self.weight = weight
        self.delay = delay

    def __repr__(self) -> str:
        return (
            f"Synapse({self.pre_idx}->{self.post_idx}, "
            f"w={self.weight:.3f}, d={self.delay})"
        )


# ── SNN Topology ─────────────────────────────────────────────────────────────


class SNNTopology:
    """Complete SNN graph: neurons, synapses, input/output mappings.

    This is a pure data structure — no simulation logic.  Hand it to
    NeuromorphicEnforcer.evaluate() to run.
    """

    def __init__(self) -> None:
        self.neurons: list[LIFNeuron] = []
        self.synapses: list[Synapse] = []
        self.input_neurons: list[int] = []
        self.output_neurons: dict[str, int] = {}
        self._compiled_at: float = time.monotonic()

    def add_neuron(
        self,
        name: str,
        threshold: float = 1.0,
        leak_rate: float = 0.2,
        reset_v: float = 0.0,
    ) -> int:
        """Add a neuron and return its index."""
        idx = len(self.neurons)
        neuron = LIFNeuron(
            idx=idx,
            name=name,
            threshold=threshold,
            leak_rate=leak_rate,
            reset_v=reset_v,
        )
        self.neurons.append(neuron)
        return idx

    def add_synapse(
        self,
        pre: int,
        post: int,
        weight: float,
        delay: int = 0,
    ) -> None:
        """Add a synapse between two neurons."""
        if pre < 0 or pre >= len(self.neurons):
            raise ValueError(f"pre index {pre} out of range [0, {len(self.neurons)})")
        if post < 0 or post >= len(self.neurons):
            raise ValueError(f"post index {post} out of range [0, {len(self.neurons)})")
        self.synapses.append(Synapse(pre, post, weight, delay))

    @property
    def neuron_count(self) -> int:
        return len(self.neurons)

    @property
    def synapse_count(self) -> int:
        return len(self.synapses)

    def topology_hash(self) -> str:
        """SHA-256 of the topology structure (deterministic)."""
        parts: list[str] = []
        for n in self.neurons:
            parts.append(f"N:{n.idx}:{n.name}:{n.threshold}:{n.leak_rate}")
        for s in self.synapses:
            parts.append(f"S:{s.pre_idx}:{s.post_idx}:{s.weight}:{s.delay}")
        for label, idx in sorted(self.output_neurons.items()):
            parts.append(f"O:{label}:{idx}")
        for idx in sorted(self.input_neurons):
            parts.append(f"I:{idx}")
        blob = "|".join(parts).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def reset_all(self) -> None:
        """Reset all neurons to initial state."""
        for n in self.neurons:
            n.reset()

    def __repr__(self) -> str:
        return (
            f"SNNTopology(neurons={self.neuron_count}, "
            f"synapses={self.synapse_count}, "
            f"inputs={len(self.input_neurons)}, "
            f"outputs={list(self.output_neurons.keys())})"
        )


# ── Policy-to-SNN Compiler ──────────────────────────────────────────────────


# Supported policy constraint types and their compiler methods
_POLICY_TYPE_MAP = {
    "budget_cap": "_compile_budget_cap",
    "rate_limit": "_compile_rate_limit",
    "amplification_gate": "_compile_amplification_gate",
    "degradation_ladder": "_compile_degradation_ladder",
    "token_cap": "_compile_token_cap",
}


class PolicyToSNNCompiler:
    """Compiles a list of policy dicts into an SNN topology.

    Each policy type maps to a small subnetwork (3-8 neurons).  The
    compiler adds shared output neurons (deny, allow, degrade) and
    wires each policy subnetwork's output to the appropriate decision.

    Usage::

        compiler = PolicyToSNNCompiler()
        topology = compiler.compile(policies)
        # topology.topology_hash() is the cache key
    """

    def compile(self, policies: list[dict]) -> SNNTopology:
        """Compile a list of policy constraint dicts into an SNN topology.

        Each dict must have a ``"type"`` key matching a supported policy
        type and a ``"config"`` dict with type-specific parameters.

        Returns a fully wired :class:`SNNTopology` ready for simulation.
        """
        t0 = time.monotonic()
        topology = SNNTopology()

        # Shared output decision neurons — high threshold so they only
        # fire when strongly driven by policy subnetworks.
        deny_idx = topology.add_neuron("out:deny", threshold=0.8, leak_rate=0.1)
        allow_idx = topology.add_neuron("out:allow", threshold=0.3, leak_rate=0.3)
        degrade_idx = topology.add_neuron("out:degrade", threshold=0.6, leak_rate=0.15)

        topology.output_neurons["deny"] = deny_idx
        topology.output_neurons["allow"] = allow_idx
        topology.output_neurons["degrade"] = degrade_idx

        # Default bias: allow neuron gets a small constant drive so that
        # if no policy triggers, the network defaults to "allow".
        bias_idx = topology.add_neuron("bias:allow", threshold=0.1, leak_rate=0.0)
        topology.add_synapse(bias_idx, allow_idx, weight=0.15)
        topology.input_neurons.append(bias_idx)

        for policy in policies:
            ptype = policy.get("type", "")
            config = policy.get("config", {})
            method_name = _POLICY_TYPE_MAP.get(ptype)
            if method_name is None:
                logger.warning("Unknown policy type %r — skipping", ptype)
                continue
            method = getattr(self, method_name)
            method(topology, config)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Compiled %d policies -> %d neurons, %d synapses in %.1f ms",
            len(policies),
            topology.neuron_count,
            topology.synapse_count,
            elapsed_ms,
        )
        return topology

    # ── Budget Cap ───────────────────────────────────────────────────────

    def _compile_budget_cap(
        self, topology: SNNTopology, config: dict,
    ) -> None:
        """Budget cap: input(spend_rate) -> comparator -> deny.

        Config keys:
            max_usd (float): budget cap in USD
            window_hours (int): evaluation window (informational)
        """
        max_usd = float(config.get("max_usd", 100.0))

        # Input neuron: firing rate encodes spend / max_usd ratio
        inp = topology.add_neuron(
            "budget:input",
            threshold=0.5,
            leak_rate=0.1,
        )
        topology.input_neurons.append(inp)

        # Comparator: fires when spend approaches cap
        comp = topology.add_neuron(
            "budget:comparator",
            threshold=0.7,
            leak_rate=0.15,
        )

        # Wire input -> comparator with weight proportional to sensitivity
        # Higher cap = lower weight (less sensitive)
        weight = min(2.0, 100.0 / max(max_usd, 1.0))
        topology.add_synapse(inp, comp, weight=weight)

        # Comparator -> deny output
        deny_idx = topology.output_neurons["deny"]
        topology.add_synapse(comp, deny_idx, weight=1.0)

        # Comparator inhibits allow output
        allow_idx = topology.output_neurons["allow"]
        topology.add_synapse(comp, allow_idx, weight=-1.5)

    # ── Rate Limit ───────────────────────────────────────────────────────

    def _compile_rate_limit(
        self, topology: SNNTopology, config: dict,
    ) -> None:
        """Rate limit: counter chain -> threshold -> deny.

        Config keys:
            max_calls (int): max calls per window
            window_seconds (int): evaluation window
        """
        max_calls = int(config.get("max_calls", 100))

        # Input neuron: firing rate encodes calls / max_calls ratio
        inp = topology.add_neuron(
            "rate:input",
            threshold=0.4,
            leak_rate=0.1,
        )
        topology.input_neurons.append(inp)

        # Counter neuron: accumulates spikes from input
        counter = topology.add_neuron(
            "rate:counter",
            threshold=0.6,
            leak_rate=0.05,  # Low leak = good memory
        )
        topology.add_synapse(inp, counter, weight=1.2)

        # Threshold neuron: fires when counter exceeds limit
        thresh = topology.add_neuron(
            "rate:threshold",
            threshold=0.8,
            leak_rate=0.1,
        )
        weight = min(2.0, 50.0 / max(max_calls, 1))
        topology.add_synapse(counter, thresh, weight=weight)

        # Wire to deny
        deny_idx = topology.output_neurons["deny"]
        topology.add_synapse(thresh, deny_idx, weight=1.0)

        # Inhibit allow
        allow_idx = topology.output_neurons["allow"]
        topology.add_synapse(thresh, allow_idx, weight=-1.5)

    # ── Amplification Gate ───────────────────────────────────────────────

    def _compile_amplification_gate(
        self, topology: SNNTopology, config: dict,
    ) -> None:
        """Amplification gate: blocks calls with excessive amplification.

        Config keys:
            max_amplification (float): max tool-call amplification factor
        """
        max_amp = float(config.get("max_amplification", 10.0))

        inp = topology.add_neuron(
            "amp:input",
            threshold=0.4,
            leak_rate=0.1,
        )
        topology.input_neurons.append(inp)

        # Detector: fires when amplification is high
        detector = topology.add_neuron(
            "amp:detector",
            threshold=0.7,
            leak_rate=0.15,
        )
        weight = min(2.5, 10.0 / max(max_amp, 1.0))
        topology.add_synapse(inp, detector, weight=weight)

        # Wire to deny
        deny_idx = topology.output_neurons["deny"]
        topology.add_synapse(detector, deny_idx, weight=0.9)

        # Inhibit allow
        allow_idx = topology.output_neurons["allow"]
        topology.add_synapse(detector, allow_idx, weight=-1.2)

    # ── Degradation Ladder ───────────────────────────────────────────────

    def _compile_degradation_ladder(
        self, topology: SNNTopology, config: dict,
    ) -> None:
        """Degradation ladder: multiple thresholds -> WTA -> model selection.

        Creates a winner-take-all (WTA) circuit where the highest-firing
        threshold neuron inhibits all others, selecting the appropriate
        degradation tier.

        Config keys:
            thresholds (list[dict]): each with "pct" (0-1) and "model" (str)
                Example: [{"pct": 0.5, "model": "gpt-4o-mini"},
                          {"pct": 0.8, "model": "gpt-3.5-turbo"}]
        """
        thresholds = config.get("thresholds", [])
        if not thresholds:
            thresholds = [
                {"pct": 0.5, "model": "gpt-4o-mini"},
                {"pct": 0.8, "model": "gpt-3.5-turbo"},
            ]

        # Sort by pct ascending
        thresholds = sorted(thresholds, key=lambda t: t.get("pct", 0))

        # Shared input for spend ratio
        inp = topology.add_neuron(
            "degrade:input",
            threshold=0.3,
            leak_rate=0.1,
        )
        topology.input_neurons.append(inp)

        # Create one detector per threshold tier
        tier_neurons: list[int] = []
        for i, tier in enumerate(thresholds):
            pct = float(tier.get("pct", 0.5))
            detector = topology.add_neuron(
                f"degrade:tier_{i}",
                threshold=pct,
                leak_rate=0.1,
            )
            topology.add_synapse(inp, detector, weight=1.5)
            tier_neurons.append(detector)

        # Winner-take-all: each tier inhibits all lower-priority tiers
        for i, n_i in enumerate(tier_neurons):
            for j, n_j in enumerate(tier_neurons):
                if i != j and i > j:
                    # Higher tier inhibits lower tier
                    topology.add_synapse(n_i, n_j, weight=-2.0)

        # The highest-firing tier drives degrade output
        degrade_idx = topology.output_neurons["degrade"]
        for n_idx in tier_neurons:
            topology.add_synapse(n_idx, degrade_idx, weight=0.8)

        # Any tier firing inhibits allow
        allow_idx = topology.output_neurons["allow"]
        for n_idx in tier_neurons:
            topology.add_synapse(n_idx, allow_idx, weight=-1.0)

    # ── Token Cap ────────────────────────────────────────────────────────

    def _compile_token_cap(
        self, topology: SNNTopology, config: dict,
    ) -> None:
        """Token cap: input(token_count) -> threshold -> deny.

        Config keys:
            max_tokens (int): max tokens per call or session
        """
        max_tokens = int(config.get("max_tokens", 10000))

        inp = topology.add_neuron(
            "token:input",
            threshold=0.4,
            leak_rate=0.1,
        )
        topology.input_neurons.append(inp)

        # Comparator
        comp = topology.add_neuron(
            "token:comparator",
            threshold=0.7,
            leak_rate=0.15,
        )
        weight = min(2.0, 5000.0 / max(max_tokens, 1))
        topology.add_synapse(inp, comp, weight=weight)

        # Wire to deny
        deny_idx = topology.output_neurons["deny"]
        topology.add_synapse(comp, deny_idx, weight=1.0)

        # Inhibit allow
        allow_idx = topology.output_neurons["allow"]
        topology.add_synapse(comp, allow_idx, weight=-1.5)
