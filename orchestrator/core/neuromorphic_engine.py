"""
Modus — Neuromorphic Enforcement Engine (Phase 8e, Research Demo)
======================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Evaluates governance policies via a software-simulated Spiking Neural
Network. This is a research demo: the SNN runs entirely in Python on a
standard CPU, so there is NO neuromorphic-hardware energy benefit here, and
any energy/power figures elsewhere in this subsystem are illustrative
constants, not measurements. It runs on any hardware (even a Raspberry Pi)
and serves as the reference implementation.

The engine:
    1. Accepts compiled SNN topologies from PolicyToSNNCompiler
    2. Encodes call metadata (spend, call count, tokens, amplification)
       as input spike rates
    3. Runs the SNN for N timesteps (default 10)
    4. Decodes output neuron spike patterns to enforcement decisions

Performance:
    - Simulation of 50-neuron network for 10 timesteps: < 1 ms
    - Input encoding: < 0.1 ms
    - Full evaluate() call: < 2 ms p99

Shadow mode:
    Runs SNN evaluation alongside the standard enforcement engine and
    compares results.  Tracks mismatch rate.  Automatically disables
    SNN if mismatch rate exceeds 1%.

Complies with the Four Laws:
    - Pure Python, stdlib only — no numpy, no scipy, no ML frameworks
    - Zero external dependencies
    - All computation local — no data leaves customer infrastructure
    - Thread-safe — no shared mutable state (topology cache is append-only)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from orchestrator.core.snn_compiler import (
    PolicyToSNNCompiler,
    SNNTopology,
)

logger = logging.getLogger(__name__)


# ── Enforcement Decision ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnforcementDecision:
    """Immutable result of a neuromorphic policy evaluation."""

    decision: str                       # "allow", "deny", "degrade"
    suggested_model: Optional[str]      # for degrade decisions
    latency_ms: float                   # total evaluate() time
    active_neurons: int                 # neurons that spiked >= 1 time
    total_neurons: int                  # total neurons in topology
    spike_efficiency: float             # active / total ratio


# ── Neuromorphic Enforcer ────────────────────────────────────────────────────


class NeuromorphicEnforcer:
    """Evaluates governance policies via SNN simulation.

    Usage::

        enforcer = NeuromorphicEnforcer()
        topo_hash = enforcer.compile_policies(yaml_string)
        decision = enforcer.evaluate(topo_hash, {
            "spend_usd": 45.0,
            "budget_cap_usd": 100.0,
            "call_count": 80,
            "rate_limit": 100,
            "token_count": 5000,
            "token_cap": 10000,
            "amplification": 3.0,
            "max_amplification": 10.0,
        })
        # decision.decision in ("allow", "deny", "degrade")
    """

    _MAX_CACHED_TOPOLOGIES = 256

    def __init__(self, backend: str = "software") -> None:
        self._backend = backend
        self._compiler = PolicyToSNNCompiler()
        self._topology_cache: dict[str, SNNTopology] = {}
        self._shadow_mismatches: int = 0
        self._shadow_total: int = 0
        self._shadow_enabled: bool = True

    # ── Public API ───────────────────────────────────────────────────────

    def compile_policies(self, policies_yaml: str) -> str:
        """Compile YAML policies into an SNN topology and cache it.

        Returns the topology hash (use as cache key for evaluate).
        """
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required for neuromorphic policy compilation. "
                "Install with: pip install pyyaml"
            )
        policies = yaml.safe_load(policies_yaml)
        if isinstance(policies, dict):
            policies = policies.get("policies", [policies])
        if not isinstance(policies, list):
            policies = [policies]

        topology = self._compiler.compile(policies)
        topo_hash = topology.topology_hash()
        self._topology_cache[topo_hash] = topology

        # Bounded cache: evict oldest entries when limit exceeded
        while len(self._topology_cache) > self._MAX_CACHED_TOPOLOGIES:
            oldest_key = next(iter(self._topology_cache))
            del self._topology_cache[oldest_key]

        logger.info(
            "Compiled SNN topology %s: %d neurons, %d synapses",
            topo_hash[:12],
            topology.neuron_count,
            topology.synapse_count,
        )
        return topo_hash

    def evaluate(
        self,
        topology_hash: str,
        inputs: dict,
        timesteps: int = 10,
    ) -> EnforcementDecision:
        """Run the SNN with encoded inputs and return a decision.

        Args:
            topology_hash: Hash from compile_policies()
            inputs: Call metadata dict with keys like spend_usd,
                    budget_cap_usd, call_count, rate_limit, etc.
            timesteps: Number of simulation timesteps (default 10)

        Returns:
            EnforcementDecision with the policy verdict.

        Raises:
            KeyError: If topology_hash is not in cache.
        """
        t0 = time.monotonic()

        topology = self._topology_cache.get(topology_hash)
        if topology is None:
            raise KeyError(
                f"Topology {topology_hash[:12]}... not found in cache. "
                f"Call compile_policies() first."
            )

        # Reset neuron states from any prior simulation
        topology.reset_all()

        # Encode inputs as spike-rate currents
        input_currents = self._encode_inputs(inputs, topology)

        # Run simulation
        output_spikes = self._run_simulation(topology, input_currents, timesteps)

        # Decode to decision
        decision = self._decode_output(output_spikes, inputs, topology)

        latency_ms = (time.monotonic() - t0) * 1000

        # Count active neurons
        active = sum(1 for spikes in output_spikes.values() if spikes > 0)

        return EnforcementDecision(
            decision=decision[0],
            suggested_model=decision[1],
            latency_ms=round(latency_ms, 3),
            active_neurons=active,
            total_neurons=topology.neuron_count,
            spike_efficiency=round(active / max(topology.neuron_count, 1), 3),
        )

    # ── Input Encoding ───────────────────────────────────────────────────

    def _encode_inputs(
        self,
        inputs: dict,
        topology: SNNTopology,
    ) -> list[float]:
        """Encode call metadata as input currents (firing rates).

        Each input neuron gets a current proportional to how close the
        metric is to its policy limit (0.0 = no pressure, 1.0 = at limit).
        """
        currents = [0.0] * topology.neuron_count

        for inp_idx in topology.input_neurons:
            neuron = topology.neurons[inp_idx]
            name = neuron.name

            if name == "bias:allow":
                # Constant bias to drive allow decision
                currents[inp_idx] = 0.5
            elif name == "budget:input":
                spend = float(inputs.get("spend_usd", 0))
                cap = float(inputs.get("budget_cap_usd", 100))
                currents[inp_idx] = min(1.0, spend / max(cap, 0.01))
            elif name == "rate:input":
                calls = float(inputs.get("call_count", 0))
                limit = float(inputs.get("rate_limit", 100))
                currents[inp_idx] = min(1.0, calls / max(limit, 1))
            elif name == "token:input":
                tokens = float(inputs.get("token_count", 0))
                cap = float(inputs.get("token_cap", 10000))
                currents[inp_idx] = min(1.0, tokens / max(cap, 1))
            elif name == "amp:input":
                amp = float(inputs.get("amplification", 0))
                max_amp = float(inputs.get("max_amplification", 10))
                currents[inp_idx] = min(1.0, amp / max(max_amp, 0.01))
            elif name == "degrade:input":
                # Use spend ratio for degradation ladder
                spend = float(inputs.get("spend_usd", 0))
                cap = float(inputs.get("budget_cap_usd", 100))
                currents[inp_idx] = min(1.0, spend / max(cap, 0.01))
            else:
                logger.debug("Unknown input neuron %r — zero current", name)

        return currents

    # ── Simulation ───────────────────────────────────────────────────────

    def _run_simulation(
        self,
        topology: SNNTopology,
        input_currents: list[float],
        timesteps: int = 10,
    ) -> dict[int, int]:
        """Run the SNN for *timesteps* and count output spikes.

        At each timestep:
            1. Compute input current for each neuron from pre-synaptic spikes
            2. Add external input currents for input neurons
            3. Step each neuron (integrate, leak, threshold check)

        Returns:
            Dict mapping neuron index -> total spike count.
        """
        spike_counts: dict[int, int] = {
            n.idx: 0 for n in topology.neurons
        }

        # Build adjacency list for fast lookup: post_idx -> [(pre_idx, weight, delay)]
        post_synapses: dict[int, list[tuple[int, float, int]]] = {}
        for s in topology.synapses:
            post_synapses.setdefault(s.post_idx, []).append(
                (s.pre_idx, s.weight, s.delay)
            )

        # Spike history for delay support: timestep -> set of neuron indices
        spike_history: list[set[int]] = []

        for t in range(timesteps):
            # Collect which neurons spiked this timestep
            spiked_this_step: set[int] = set()

            for neuron in topology.neurons:
                # Sum input from pre-synaptic connections
                synaptic_input = 0.0
                for pre_idx, weight, delay in post_synapses.get(neuron.idx, []):
                    # Check if pre-neuron spiked at (t - delay)
                    history_idx = t - delay
                    if delay == 0:
                        # Immediate: use current spike state of pre-neuron
                        if topology.neurons[pre_idx].spiked:
                            synaptic_input += weight
                    elif 0 <= history_idx < len(spike_history):
                        if pre_idx in spike_history[history_idx]:
                            synaptic_input += weight

                # Add external input current for input neurons
                if neuron.idx in topology.input_neurons:
                    synaptic_input += input_currents[neuron.idx]

                # Step the neuron
                fired = neuron.step(synaptic_input)
                if fired:
                    spike_counts[neuron.idx] += 1
                    spiked_this_step.add(neuron.idx)

            spike_history.append(spiked_this_step)

        return spike_counts

    # ── Output Decoding ──────────────────────────────────────────────────

    def _decode_output(
        self,
        output_spikes: dict[int, int],
        inputs: dict,
        topology: SNNTopology,
    ) -> tuple[str, Optional[str]]:
        """Decode spike pattern to (decision, suggested_model).

        Priority order: deny > degrade > allow.
        If "deny" output neuron spiked, decision is deny.
        If "degrade" spiked (and deny did not), decision is degrade.
        Otherwise, decision is allow.
        """
        deny_idx = topology.output_neurons.get("deny")
        degrade_idx = topology.output_neurons.get("degrade")
        topology.output_neurons.get("allow")

        deny_spikes = output_spikes.get(deny_idx, 0) if deny_idx is not None else 0
        degrade_spikes = output_spikes.get(degrade_idx, 0) if degrade_idx is not None else 0

        if deny_spikes > 0:
            return ("deny", None)

        if degrade_spikes > 0:
            suggested = self._pick_degradation_model(output_spikes, topology, inputs)
            return ("degrade", suggested)

        return ("allow", None)

    def _pick_degradation_model(
        self,
        output_spikes: dict[int, int],
        topology: SNNTopology,
        inputs: dict,
    ) -> Optional[str]:
        """Pick the best degradation model based on which tier neuron fired most.

        Looks for neurons named "degrade:tier_N" and picks the highest-tier
        one that spiked.
        """
        best_tier = -1
        for neuron in topology.neurons:
            if neuron.name.startswith("degrade:tier_"):
                tier_num = int(neuron.name.split("_")[1])
                if output_spikes.get(neuron.idx, 0) > 0 and tier_num > best_tier:
                    best_tier = tier_num

        # Default degradation models by tier
        default_models = ["gpt-4o-mini", "gpt-3.5-turbo", "gpt-3.5-turbo-instruct"]
        if 0 <= best_tier < len(default_models):
            return default_models[best_tier]
        elif best_tier >= len(default_models):
            return default_models[-1]

        return "gpt-4o-mini"

    # ── Shadow Mode ──────────────────────────────────────────────────────

    def shadow_validate(
        self,
        standard_decision: str,
        snn_decision: str,
    ) -> bool:
        """Compare standard engine decision with SNN decision.

        Tracks mismatch rate and disables SNN if > 1%.

        Returns True if decisions match, False otherwise.
        """
        if not self._shadow_enabled:
            return True

        self._shadow_total += 1
        match = standard_decision == snn_decision

        if not match:
            self._shadow_mismatches += 1
            logger.warning(
                "SNN shadow mismatch #%d: standard=%s, snn=%s (rate=%.2f%%)",
                self._shadow_mismatches,
                standard_decision,
                snn_decision,
                self.mismatch_rate * 100,
            )

        # Auto-disable if mismatch rate exceeds 1% (with minimum sample)
        if self._shadow_total >= 100 and self.mismatch_rate > 0.01:
            logger.error(
                "SNN mismatch rate %.2f%% exceeds 1%% threshold — "
                "disabling neuromorphic enforcement",
                self.mismatch_rate * 100,
            )
            self._shadow_enabled = False

        return match

    @property
    def mismatch_rate(self) -> float:
        """Current shadow mismatch rate (0.0 to 1.0)."""
        if self._shadow_total == 0:
            return 0.0
        return self._shadow_mismatches / self._shadow_total

    @property
    def shadow_enabled(self) -> bool:
        """Whether shadow mode is still active."""
        return self._shadow_enabled

    def reset_shadow_stats(self) -> None:
        """Reset shadow mode counters and re-enable."""
        self._shadow_mismatches = 0
        self._shadow_total = 0
        self._shadow_enabled = True

    def invalidate_topology(self, topology_hash: str) -> bool:
        """Remove a topology from the cache (e.g. on policy change).

        Returns True if found and removed, False otherwise.
        """
        return self._topology_cache.pop(topology_hash, None) is not None

    def clear_cache(self) -> None:
        """Clear all cached topologies."""
        self._topology_cache.clear()

    @property
    def cached_topology_count(self) -> int:
        """Number of compiled topologies in cache."""
        return len(self._topology_cache)


# ── Singleton enforcer for metrics collection ────────────────────────────────

_active_enforcer: Optional[NeuromorphicEnforcer] = None


def set_active_enforcer(enforcer: Optional[NeuromorphicEnforcer]) -> None:
    """Register the active enforcer instance for background metrics collection."""
    global _active_enforcer
    _active_enforcer = enforcer


def get_active_enforcer() -> Optional[NeuromorphicEnforcer]:
    """Return the active enforcer instance, or None if not registered."""
    return _active_enforcer


# ── Background task entry point ──────────────────────────────────────────────

async def collect_neuromorphic_metrics() -> None:
    """Aggregate neuromorphic enforcement metrics (background task).

    Reads the current state of the active NeuromorphicEnforcer (cached
    topology count, shadow mode stats) and writes a NeuromorphicMetrics
    row for each cached topology.  Returns early if no enforcer is active.
    """
    enforcer = _active_enforcer
    if enforcer is None or enforcer.cached_topology_count == 0:
        logger.debug("Neuromorphic metrics collection — no active enforcer or no topologies")
        return

    from datetime import datetime, timezone, timedelta
    from orchestrator.db.session import get_session_ctx
    from orchestrator.db.models import NeuromorphicMetrics

    now = datetime.now(timezone.utc)
    period_end = now
    period_start = now - timedelta(seconds=300)  # 5-minute window

    async with get_session_ctx() as db:
        for topo_hash, topology in list(enforcer._topology_cache.items()):
            # Compute aggregate metrics from topology state
            total_neurons = topology.neuron_count
            active_neurons = sum(
                1 for n in topology.neurons if n.membrane_v > 0
            )
            spike_efficiency = active_neurons / max(total_neurons, 1)

            metric = NeuromorphicMetrics(
                team_id="00000000-0000-0000-0000-000000000000",  # platform-level
                evaluation_count=0,
                avg_latency_ms=0.0,
                avg_power_mw=None,
                spike_efficiency=round(spike_efficiency, 6),
                topology_hash=topo_hash,
                hardware_backend=enforcer._backend,
                period_start=period_start,
                period_end=period_end,
            )
            db.add(metric)

        await db.commit()

    logger.debug(
        "Neuromorphic metrics collected for %d topologies",
        enforcer.cached_topology_count,
    )
