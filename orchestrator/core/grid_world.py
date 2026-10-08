"""
Modus — Grid World Agent Simulator (Phase 8d)
====================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Lightweight agent simulation environment for fitness evaluation of
policy genomes. Each simulated agent replays a trajectory drawn from
historical usage distributions, and the policy genome is applied at
each step to determine allow/deny/degrade outcomes.

The simulator computes fitness metrics:
  - cost_saved: total cost prevented by policy enforcement
  - violations_caught: number of policy violations correctly blocked
  - false_positives: legitimate calls incorrectly blocked
  - Fitness = cost_saved * 0.5 + violations_caught * 0.3 - false_positives * 0.2

Design constraints:
  - Pure Python stdlib only — no numpy, no scipy, no ML libraries
  - Bounded: max 500 agents, max 50 steps per trajectory
  - Deterministic via random.Random instance for reproducibility
  - All computation local — no data leaves the customer's infrastructure
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

from orchestrator.core.policy_genome import (
    PolicyGenome,
    FloatGene,
    IntGene,
    CategoricalGene,
    BoolGene,
)

logger = logging.getLogger(__name__)

# ── Hard bounds (never exceed) ───────────────────────────────────────────────

MAX_AGENTS = 500
MAX_STEPS = 50
DEFAULT_AGENTS = 200
DEFAULT_STEPS = 50

# ── Fitness weights ──────────────────────────────────────────────────────────

WEIGHT_COST_SAVED = 0.5
WEIGHT_VIOLATIONS_CAUGHT = 0.3
WEIGHT_FALSE_POSITIVES = 0.2


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SimulationResult:
    """Immutable result of a grid world simulation run."""
    cost_saved: float
    violations_caught: int
    false_positives: int
    total_calls: int
    total_cost: float
    elapsed_ms: int
    fitness: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UsagePattern:
    """
    Describes the usage distribution for one model, derived from
    historical aggregates. Used to generate agent trajectories.
    """
    model: str
    avg_tokens: float
    std_tokens: float
    avg_cost: float
    call_count: int
    # Derived: probability weight for trajectory sampling
    weight: float = 0.0

    @classmethod
    def from_aggregate(cls, agg: dict) -> UsagePattern:
        """Parse a usage aggregate dict into a UsagePattern."""
        return cls(
            model=str(agg.get("model", "unknown")),
            avg_tokens=float(agg.get("avg_tokens", 500)),
            std_tokens=float(agg.get("std_tokens", 100)),
            avg_cost=float(agg.get("avg_cost", 0.01)),
            call_count=int(agg.get("call_count", 100)),
        )


@dataclass
class TrajectoryStep:
    """One step in an agent's trajectory."""
    model: str
    tokens: int
    cost: float
    # Whether this step represents genuinely excessive/abusive usage
    is_violation: bool = False


@dataclass
class PolicyDecision:
    """Result of applying a genome to a single trajectory step."""
    allowed: bool
    reason: str = ""


# ── Trajectory generation ───────────────────────────────────────────────────

def _generate_trajectory(
    patterns: List[UsagePattern],
    steps: int,
    rng: random.Random,
    violation_rate: float = 0.15,
) -> List[TrajectoryStep]:
    """
    Generate a realistic agent trajectory from usage patterns.

    Each step samples a model from the weighted distribution, then
    generates token count and cost from the model's distribution.
    A fraction of steps (violation_rate) are marked as violations —
    these represent genuinely excessive or abusive usage that should
    be blocked by good policies.
    """
    if not patterns:
        return []

    # Compute sampling weights from call counts
    total_calls = sum(p.call_count for p in patterns)
    if total_calls == 0:
        weights = [1.0 / len(patterns)] * len(patterns)
    else:
        weights = [p.call_count / total_calls for p in patterns]

    trajectory: List[TrajectoryStep] = []
    for _ in range(steps):
        # Weighted random model selection
        pattern = _weighted_choice(patterns, weights, rng)

        # Sample tokens (clamp to positive)
        tokens = max(1, int(rng.gauss(pattern.avg_tokens, pattern.std_tokens)))

        # Cost proportional to tokens (with noise)
        token_ratio = tokens / max(1.0, pattern.avg_tokens)
        cost = max(0.0001, pattern.avg_cost * token_ratio * rng.uniform(0.8, 1.2))

        # Determine if this is a violation (excessive usage)
        is_violation = rng.random() < violation_rate

        if is_violation:
            # Violations use more tokens and cost
            tokens = int(tokens * rng.uniform(2.0, 5.0))
            cost = cost * rng.uniform(2.0, 5.0)

        trajectory.append(TrajectoryStep(
            model=pattern.model,
            tokens=tokens,
            cost=round(cost, 6),
            is_violation=is_violation,
        ))

    return trajectory


def _weighted_choice(
    items: List[UsagePattern],
    weights: List[float],
    rng: random.Random,
) -> UsagePattern:
    """Weighted random selection without the `random.choices` API."""
    total = sum(weights)
    if total <= 0:
        return items[0]
    r = rng.random() * total
    cumulative = 0.0
    for item, w in zip(items, weights):
        cumulative += w
        if r <= cumulative:
            return item
    return items[-1]


# ── Policy enforcement simulation ────────────────────────────────────────────

def _apply_genome_to_step(
    genome: PolicyGenome,
    step: TrajectoryStep,
    cumulative_cost: float,
    cumulative_calls: int,
    cumulative_tokens: int,
) -> PolicyDecision:
    """
    Apply a policy genome's parameters to a single trajectory step.

    Checks all encoded policies against the current cumulative state
    and step parameters. Returns whether the call would be allowed.
    """
    gene_map = {g.name: g for g in genome.genes}

    for gene in genome.genes:
        name = gene.name

        # Budget cap check: deny if cumulative cost exceeds cap
        if name.endswith(".cap_usd") and isinstance(gene, FloatGene):
            if cumulative_cost + step.cost > gene.value():
                return PolicyDecision(
                    allowed=False,
                    reason=f"budget_cap: cost {cumulative_cost + step.cost:.4f} > cap {gene.value():.4f}",
                )

        # Rate limit check: deny if cumulative calls exceed max
        if name.endswith(".max_calls") and isinstance(gene, IntGene):
            if cumulative_calls + 1 > gene.value():
                return PolicyDecision(
                    allowed=False,
                    reason=f"rate_limit: calls {cumulative_calls + 1} > max {gene.value()}",
                )

        # Token cap check: deny if cumulative tokens exceed max
        if name.endswith(".max_tokens") and isinstance(gene, IntGene):
            if cumulative_tokens + step.tokens > gene.value():
                return PolicyDecision(
                    allowed=False,
                    reason=f"token_cap: tokens {cumulative_tokens + step.tokens} > max {gene.value()}",
                )

        # Amplification gate: deny if calls exceed amplification threshold
        if name.endswith(".max_amplification") and isinstance(gene, FloatGene):
            # Amplification = actual calls / expected calls (assume 3 is normal)
            amplification = (cumulative_calls + 1) / 3.0
            if amplification > gene.value():
                return PolicyDecision(
                    allowed=False,
                    reason=f"amplification_gate: amp {amplification:.2f} > max {gene.value():.2f}",
                )

        # Degradation ladder: check budget percentage thresholds
        if name.endswith(".budget_usd") and isinstance(gene, FloatGene):
            budget = gene.value()
            if budget > 0:
                spend_pct = ((cumulative_cost + step.cost) / budget) * 100
                prefix = name.rsplit(".budget_usd", 1)[0]
                # Check deny tiers
                for tier_gene in genome.genes:
                    if (tier_gene.name.startswith(prefix + ".tier_")
                            and tier_gene.name.endswith(".action")
                            and isinstance(tier_gene, CategoricalGene)):
                        tier_idx = tier_gene.name.split(".tier_")[1].split(".")[0]
                        pct_gene = gene_map.get(f"{prefix}.tier_{tier_idx}.pct")
                        if pct_gene and isinstance(pct_gene, FloatGene):
                            if spend_pct >= pct_gene.value() and tier_gene.value() == "deny":
                                return PolicyDecision(
                                    allowed=False,
                                    reason=f"degradation_ladder: spend {spend_pct:.1f}% >= deny tier {pct_gene.value():.1f}%",
                                )

        # Model denylist: deny if model is in the list
        if ".model_" in name and name.endswith(tuple(str(i) for i in range(20))):
            # Check if this is a denylist gene and the model matches
            if "denylist" in name and isinstance(gene, CategoricalGene):
                if step.model == gene.value():
                    # Check enabled flag
                    prefix = name.rsplit(".model_", 1)[0]
                    enabled_gene = gene_map.get(f"{prefix}.enabled")
                    if enabled_gene is None or (isinstance(enabled_gene, BoolGene) and enabled_gene.value()):
                        return PolicyDecision(
                            allowed=False,
                            reason=f"model_denylist: {step.model} is denied",
                        )

    return PolicyDecision(allowed=True)


# ── GridWorldSimulator ───────────────────────────────────────────────────────

class GridWorldSimulator:
    """
    Lightweight agent simulation environment for policy genome fitness
    evaluation. Runs multiple agents against a policy genome and measures
    cost savings, violation detection, and false positive rates.

    Parameters
    ----------
    agent_count : int
        Number of simulated agents (clamped to MAX_AGENTS=500).
    max_steps : int
        Maximum trajectory length per agent (clamped to MAX_STEPS=50).
    rng : random.Random
        Random number generator for reproducibility.
    """

    def __init__(
        self,
        agent_count: int = DEFAULT_AGENTS,
        max_steps: int = DEFAULT_STEPS,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.agent_count = min(agent_count, MAX_AGENTS)
        self.max_steps = min(max_steps, MAX_STEPS)
        self.rng = rng or random.Random()
        self._patterns: List[UsagePattern] = []

    def load_usage_patterns(self, aggregates: List[dict]) -> None:
        """
        Load historical usage data as simulation distributions.

        Parameters
        ----------
        aggregates : list[dict]
            Each dict has: model, avg_tokens, std_tokens, avg_cost, call_count.
            Example::

                {"model": "gpt-4", "avg_tokens": 500, "std_tokens": 100,
                 "avg_cost": 0.05, "call_count": 1000}
        """
        self._patterns = []
        for agg in aggregates:
            try:
                pattern = UsagePattern.from_aggregate(agg)
                if pattern.call_count > 0:
                    self._patterns.append(pattern)
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping invalid aggregate: %s (%s)", agg, exc)

        if not self._patterns:
            # Fallback: create a minimal default pattern
            self._patterns = [
                UsagePattern(
                    model="gpt-4o-mini",
                    avg_tokens=500,
                    std_tokens=100,
                    avg_cost=0.01,
                    call_count=100,
                ),
            ]

        logger.debug(
            "Loaded %d usage patterns for simulation", len(self._patterns)
        )

    def simulate(self, genome: PolicyGenome) -> SimulationResult:
        """
        Run all agents against the given policy genome and compute
        fitness metrics.

        Each agent replays a trajectory drawn from the loaded usage
        distributions. The genome's policy parameters are applied at
        each step to determine if the call is allowed or denied.

        Parameters
        ----------
        genome : PolicyGenome
            The policy genome to evaluate.

        Returns
        -------
        SimulationResult
            Aggregate metrics including fitness score.
        """
        t0 = time.perf_counter()

        total_cost_saved = 0.0
        total_violations_caught = 0
        total_false_positives = 0
        total_calls = 0
        total_cost = 0.0

        for agent_idx in range(self.agent_count):
            # Generate trajectory for this agent
            # Vary trajectory length per agent for realism
            steps = max(
                1,
                min(self.max_steps, int(self.rng.gauss(self.max_steps * 0.7, self.max_steps * 0.15)))
            )
            trajectory = _generate_trajectory(
                self._patterns, steps, self.rng,
            )

            # Simulate agent executing trajectory under the genome's policies
            cumulative_cost = 0.0
            cumulative_calls = 0
            cumulative_tokens = 0

            for step in trajectory:
                total_calls += 1
                total_cost += step.cost

                decision = _apply_genome_to_step(
                    genome, step,
                    cumulative_cost, cumulative_calls, cumulative_tokens,
                )

                if decision.allowed:
                    # Call goes through — update cumulatives
                    cumulative_cost += step.cost
                    cumulative_calls += 1
                    cumulative_tokens += step.tokens
                else:
                    # Call denied by policy
                    total_cost_saved += step.cost
                    if step.is_violation:
                        # Correctly blocked a violation
                        total_violations_caught += 1
                    else:
                        # Incorrectly blocked a legitimate call
                        total_false_positives += 1

        # Compute fitness
        fitness = (
            total_cost_saved * WEIGHT_COST_SAVED
            + total_violations_caught * WEIGHT_VIOLATIONS_CAUGHT
            - total_false_positives * WEIGHT_FALSE_POSITIVES
        )

        elapsed_ms = round((time.perf_counter() - t0) * 1000)

        return SimulationResult(
            cost_saved=round(total_cost_saved, 6),
            violations_caught=total_violations_caught,
            false_positives=total_false_positives,
            total_calls=total_calls,
            total_cost=round(total_cost, 6),
            elapsed_ms=elapsed_ms,
            fitness=round(fitness, 6),
        )


# ── Convenience: batch simulation ────────────────────────────────────────────

def evaluate_population(
    simulator: GridWorldSimulator,
    population: List[PolicyGenome],
) -> List[PolicyGenome]:
    """
    Evaluate fitness for every genome in a population.

    Updates each genome's fitness attribute in-place and returns the
    population sorted by fitness (descending).

    Parameters
    ----------
    simulator : GridWorldSimulator
        Configured simulator with loaded usage patterns.
    population : list[PolicyGenome]
        Genomes to evaluate.

    Returns
    -------
    list[PolicyGenome]
        Same genomes, sorted by fitness descending.
    """
    for genome in population:
        result = simulator.simulate(genome)
        genome.fitness = result.fitness

    population.sort(key=lambda g: g.fitness, reverse=True)
    return population
