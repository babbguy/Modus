"""
Modus — Constitutional Evolution Engine (Phase 8d)
=======================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Main evolution engine that combines policy genomes, grid world simulation,
and optional Z3 formal verification into a genetic algorithm that discovers
improved governance policies from historical usage data.

Algorithm overview:
  1. Parse current policies into a seed genome
  2. Initialize population: seed + N random mutations
  3. For each generation:
     a. Evaluate fitness via GridWorldSimulator
     b. Tournament selection (size=3)
     c. Uniform crossover (rate=0.7)
     d. Per-gene mutation (rate=0.1)
     e. Elitism: top 10% carried forward unchanged
  4. Best genome: if fitness > current, generate proposal
  5. Optional Tier 2: run policy_prover on best genome YAML

Tiers:
  Tier 1 — Pure Python (stdlib only). No Z3, no ML, no heavy deps.
  Tier 2 — Z3 verification of best genome (optional, auto-detected).

Background task entry point:
  run_evolution_loop(db) — queries usage aggregates, runs evolver,
  stores EvolutionGeneration + EvolutionProposal in DB.

All computation runs locally. No data leaves the customer's
infrastructure. Thread-safe — no shared mutable state.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.grid_world import (
    GridWorldSimulator,
    evaluate_population,
)
from orchestrator.core.policy_genome import PolicyGenome
from orchestrator.db.models import (
    EvolutionGeneration,
    EvolutionProposal,
    GovernancePolicy,
    UsageAggregate,
)
from orchestrator.db.session import _session_factory

logger = logging.getLogger(__name__)

# ── CoT ledger (optional — failure must never break evolution) ────────────────

try:
    from orchestrator.core.cot_ledger import (
        append_entry as _cot_append,
        link_entry_to_proposal as _cot_link,
        infer_regulatory_tags as _cot_tags,
        build_reasoning_step as _cot_step,
    )
    _cot_available = True
except ImportError:
    _cot_available = False

# ── Check for optional policy prover ─────────────────────────────────────────

_prover_available = False
try:
    from orchestrator.core.policy_prover import prove_policy, ProofResult  # noqa: F401
    _prover_available = True
except ImportError:
    pass


# ── Configuration defaults ──────────────────────────────────────────────────

DEFAULT_POPULATION_SIZE = 100
DEFAULT_GENERATIONS = 10
DEFAULT_CROSSOVER_RATE = 0.7
DEFAULT_MUTATION_RATE = 0.1
DEFAULT_TOURNAMENT_SIZE = 3
DEFAULT_ELITISM_RATIO = 0.1
DEFAULT_AGENT_COUNT = 200
DEFAULT_SIM_STEPS = 50
EVOLUTION_LOOKBACK_HOURS = 168  # 7 days
MIN_AGGREGATES_FOR_EVOLUTION = 10


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class EvolutionResult:
    """Result of a complete evolution run."""
    best_genome: PolicyGenome
    best_fitness: float
    avg_fitness: float
    generations_run: int
    population_size: int
    constitutional_diff: str
    prover_result: Optional[dict]
    elapsed_ms: int

    def to_dict(self) -> dict:
        return {
            "best_fitness": round(self.best_fitness, 6),
            "avg_fitness": round(self.avg_fitness, 6),
            "generations_run": self.generations_run,
            "population_size": self.population_size,
            "constitutional_diff": self.constitutional_diff,
            "prover_result": self.prover_result,
            "elapsed_ms": self.elapsed_ms,
        }


# ── Selection operators ──────────────────────────────────────────────────────

def _tournament_select(
    population: List[PolicyGenome],
    tournament_size: int,
    rng: random.Random,
) -> PolicyGenome:
    """
    Tournament selection: pick tournament_size random individuals and
    return the one with the highest fitness.
    """
    if len(population) <= tournament_size:
        candidates = list(population)
    else:
        candidates = rng.sample(population, tournament_size)

    best = max(candidates, key=lambda g: g.fitness)
    return best


# ── ConstitutionalEvolver ────────────────────────────────────────────────────

class ConstitutionalEvolver:
    """
    Genetic algorithm engine for evolving governance policies.

    Parameters
    ----------
    population_size : int
        Number of genomes per generation (default 100).
    generations : int
        Number of generations to run (default 10).
    tier : str
        Proving tier: "auto" (use Z3 if available), "1" (pure Python),
        "2" (require Z3).
    crossover_rate : float
        Probability of gene swap during crossover (default 0.7).
    mutation_rate : float
        Per-gene mutation probability (default 0.1).
    tournament_size : int
        Tournament selection group size (default 3).
    elitism_ratio : float
        Fraction of top genomes carried forward unchanged (default 0.1).
    agent_count : int
        Simulated agents per fitness evaluation (default 200).
    sim_steps : int
        Max trajectory steps per agent (default 50).
    seed : int or None
        RNG seed for reproducibility.
    """

    def __init__(
        self,
        population_size: int = DEFAULT_POPULATION_SIZE,
        generations: int = DEFAULT_GENERATIONS,
        tier: str = "auto",
        crossover_rate: float = DEFAULT_CROSSOVER_RATE,
        mutation_rate: float = DEFAULT_MUTATION_RATE,
        tournament_size: int = DEFAULT_TOURNAMENT_SIZE,
        elitism_ratio: float = DEFAULT_ELITISM_RATIO,
        agent_count: int = DEFAULT_AGENT_COUNT,
        sim_steps: int = DEFAULT_SIM_STEPS,
        seed: Optional[int] = None,
    ) -> None:
        self.population_size = max(10, min(population_size, 500))
        self.generations = max(1, min(generations, 50))
        self.tier = tier
        self.crossover_rate = crossover_rate
        self.mutation_rate = mutation_rate
        self.tournament_size = max(2, tournament_size)
        self.elitism_ratio = max(0.0, min(elitism_ratio, 0.5))
        self.agent_count = agent_count
        self.sim_steps = sim_steps
        self.rng = random.Random(seed)

    def evolve(
        self,
        current_policies_yaml: str,
        usage_aggregates: List[dict],
        team_id: str,
    ) -> EvolutionResult:
        """
        Run the full genetic algorithm to evolve improved policies.

        Parameters
        ----------
        current_policies_yaml : str
            YAML text of the current governance policies.
        usage_aggregates : list[dict]
            Historical usage data for simulation (model, avg_tokens, etc.).
        team_id : str
            Team identifier for logging/tracking.

        Returns
        -------
        EvolutionResult
            Best genome, fitness metrics, constitutional diff, and
            optional prover verification result.
        """
        t0 = time.perf_counter()

        # Parse current policies into seed genome
        seed_genome = PolicyGenome.from_yaml(current_policies_yaml)
        if seed_genome.gene_count() == 0:
            logger.warning(
                "No genes extracted from current policies for team %s", team_id
            )
            return EvolutionResult(
                best_genome=seed_genome,
                best_fitness=0.0,
                avg_fitness=0.0,
                generations_run=0,
                population_size=0,
                constitutional_diff="No genes to evolve.",
                prover_result=None,
                elapsed_ms=round((time.perf_counter() - t0) * 1000),
            )

        # Configure simulator
        simulator = GridWorldSimulator(
            agent_count=self.agent_count,
            max_steps=self.sim_steps,
            rng=random.Random(self.rng.randint(0, 2**31)),
        )
        simulator.load_usage_patterns(usage_aggregates)

        # Initialize population: seed + mutations
        population = self._init_population(seed_genome)

        # Evaluate initial fitness of the seed
        seed_result = simulator.simulate(seed_genome)
        seed_genome.fitness = seed_result.fitness

        logger.info(
            "Evolution starting: team=%s genes=%d pop=%d gens=%d seed_fitness=%.4f",
            team_id, seed_genome.gene_count(), self.population_size,
            self.generations, seed_genome.fitness,
        )

        # Track best across all generations
        overall_best = seed_genome.clone()

        # Run generations
        generations_run = 0
        for gen_idx in range(self.generations):
            generations_run = gen_idx + 1

            # Evaluate fitness for entire population
            # Use a fresh RNG seed per generation for different trajectories
            sim_rng = random.Random(self.rng.randint(0, 2**31))
            gen_simulator = GridWorldSimulator(
                agent_count=self.agent_count,
                max_steps=self.sim_steps,
                rng=sim_rng,
            )
            gen_simulator.load_usage_patterns(usage_aggregates)
            population = evaluate_population(gen_simulator, population)

            # Track stats
            gen_best = population[0]
            gen_avg = sum(g.fitness for g in population) / len(population)

            if gen_best.fitness > overall_best.fitness:
                overall_best = gen_best.clone()

            logger.debug(
                "Generation %d/%d: best=%.4f avg=%.4f",
                gen_idx + 1, self.generations, gen_best.fitness, gen_avg,
            )

            # Check for early convergence (fitness hasn't improved in a while)
            # Simple heuristic: if top 3 all have same fitness, stop early
            if len(population) >= 3:
                top3 = [population[i].fitness for i in range(3)]
                if max(top3) - min(top3) < 0.001:
                    if gen_idx > self.generations // 3:
                        logger.debug(
                            "Early convergence at generation %d", gen_idx + 1
                        )
                        break

            # Create next generation
            population = self._create_next_generation(population)

        # Final evaluation of overall best
        final_sim = GridWorldSimulator(
            agent_count=self.agent_count,
            max_steps=self.sim_steps,
            rng=random.Random(self.rng.randint(0, 2**31)),
        )
        final_sim.load_usage_patterns(usage_aggregates)
        final_result = final_sim.simulate(overall_best)
        overall_best.fitness = final_result.fitness

        # Compute population stats from last evaluated generation
        avg_fitness = sum(g.fitness for g in population) / max(len(population), 1)

        # Constitutional diff: compare best evolved genome to seed
        constitutional_diff = seed_genome.diff(overall_best)

        # Optional Tier 2: Z3 verification
        prover_result = self._run_prover(overall_best)

        elapsed_ms = round((time.perf_counter() - t0) * 1000)

        logger.info(
            "Evolution complete: team=%s gens=%d best=%.4f avg=%.4f elapsed=%dms",
            team_id, generations_run, overall_best.fitness, avg_fitness, elapsed_ms,
        )

        return EvolutionResult(
            best_genome=overall_best,
            best_fitness=overall_best.fitness,
            avg_fitness=avg_fitness,
            generations_run=generations_run,
            population_size=self.population_size,
            constitutional_diff=constitutional_diff,
            prover_result=prover_result,
            elapsed_ms=elapsed_ms,
        )

    # ── Population initialization ────────────────────────────────────────

    def _init_population(self, seed: PolicyGenome) -> List[PolicyGenome]:
        """
        Create initial population: the seed genome plus N-1 mutations.
        """
        population: List[PolicyGenome] = [seed.clone()]

        for _ in range(self.population_size - 1):
            # Create mutant with higher initial mutation rate for diversity
            mutant = seed.mutate(
                mutation_rate=min(0.5, self.mutation_rate * 3),
                rng=self.rng,
            )
            population.append(mutant)

        return population

    # ── Next generation creation ─────────────────────────────────────────

    def _create_next_generation(
        self,
        population: List[PolicyGenome],
    ) -> List[PolicyGenome]:
        """
        Create the next generation via:
        1. Elitism: top N% carried forward unchanged
        2. Fill remaining slots via tournament selection + crossover + mutation
        """
        pop_size = len(population)
        elite_count = max(1, int(pop_size * self.elitism_ratio))
        next_gen: List[PolicyGenome] = []

        # Elitism: carry forward top genomes unchanged
        for i in range(elite_count):
            next_gen.append(population[i].clone())

        # Fill the rest with offspring
        while len(next_gen) < pop_size:
            # Tournament select two parents
            parent_a = _tournament_select(population, self.tournament_size, self.rng)
            parent_b = _tournament_select(population, self.tournament_size, self.rng)

            # Crossover
            child = parent_a.crossover(parent_b, self.crossover_rate, self.rng)

            # Mutate
            child = child.mutate(self.mutation_rate, self.rng)

            next_gen.append(child)

        return next_gen[:pop_size]

    # ── Optional Z3 prover integration ───────────────────────────────────

    def _run_prover(self, genome: PolicyGenome) -> Optional[dict]:
        """
        Run the policy prover on the best genome's YAML if available.

        Returns None if prover is not available or genome cannot be
        serialized to YAML.
        """
        if not _prover_available:
            if self.tier == "2":
                logger.warning(
                    "Tier 2 (Z3) requested but policy_prover not available"
                )
            return None

        if self.tier == "1":
            # Tier 1 explicitly requested — skip Z3
            return None

        try:
            genome_yaml = genome.to_yaml()
            if not genome_yaml:
                return None

            result = prove_policy(
                genome_yaml,
                policy_id="evolution_best",
                method="auto" if self.tier == "auto" else "smt",
            )
            return {
                "status": result.status,
                "proof_type": result.proof_type,
                "statement": result.statement,
                "counterexample": result.counterexample,
                "solver_time_ms": result.solver_time_ms,
                "variables_checked": result.variables_checked,
            }
        except Exception as exc:
            logger.warning("Prover failed on best genome: %s", exc)
            return {"status": "error", "error": str(exc)}


# ── Usage aggregate helpers ──────────────────────────────────────────────────

async def _fetch_usage_aggregates(
    db: AsyncSession,
    team_id: str,
    lookback_hours: int = EVOLUTION_LOOKBACK_HOURS,
) -> List[dict]:
    """
    Query UsageAggregate to build simulation input distributions.

    Groups by model and computes avg/std tokens and cost.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    q = (
        select(
            UsageAggregate.model,
            func.sum(UsageAggregate.call_count).label("total_calls"),
            func.sum(UsageAggregate.input_tokens).label("total_input"),
            func.sum(UsageAggregate.output_tokens).label("total_output"),
            func.sum(UsageAggregate.total_cost).label("total_cost"),
            func.count().label("bucket_count"),
        )
        .where(
            and_(
                UsageAggregate.team_id == team_id,
                UsageAggregate.period_start >= cutoff,
                UsageAggregate.granularity == "hourly",
            )
        )
        .group_by(UsageAggregate.model)
        .having(func.sum(UsageAggregate.call_count) >= 10)
    )

    result = await db.execute(q)
    aggregates: List[dict] = []

    for row in result.all():
        total_calls = int(row.total_calls or 0)
        if total_calls == 0:
            continue

        total_tokens = int((row.total_input or 0) + (row.total_output or 0))
        total_cost = float(row.total_cost or 0)

        avg_tokens = total_tokens / total_calls
        avg_cost = total_cost / total_calls
        # Estimate std from bucket variance (rough approximation)
        bucket_count = int(row.bucket_count or 1)
        std_tokens = avg_tokens * 0.3 if bucket_count < 5 else avg_tokens * 0.2

        aggregates.append({
            "model": row.model or "unknown",
            "avg_tokens": round(avg_tokens, 1),
            "std_tokens": round(std_tokens, 1),
            "avg_cost": round(avg_cost, 6),
            "call_count": total_calls,
        })

    return aggregates


async def _fetch_current_policies_yaml(
    db: AsyncSession,
    team_id: str,
) -> str:
    """
    Fetch all active governance policies for a team and serialize to YAML.
    """
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML required for policy serialization")
        return ""

    q = (
        select(GovernancePolicy)
        .where(
            and_(
                GovernancePolicy.team_id == team_id,
                GovernancePolicy.is_active == True,  # noqa: E712
            )
        )
        .order_by(GovernancePolicy.priority)
    )

    result = await db.execute(q)
    policies = result.scalars().all()

    if not policies:
        return ""

    policy_dicts: List[dict] = []
    for p in policies:
        pd: dict = {
            "name": p.name,
            "type": p.policy_type,
        }
        if p.effect:
            pd["effect"] = p.effect
        if p.config:
            pd["config"] = dict(p.config) if isinstance(p.config, dict) else p.config
        if p.action:
            pd["action"] = dict(p.action) if isinstance(p.action, dict) else p.action
        policy_dicts.append(pd)

    return yaml.dump(policy_dicts, default_flow_style=False, sort_keys=False)


# ── Background task entry point ──────────────────────────────────────────────

async def run_evolution_loop(db: AsyncSession) -> None:
    """
    Background task entry point for the constitutional evolution engine.

    Queries usage aggregates for each team, runs the genetic algorithm,
    and stores results (EvolutionGeneration + EvolutionProposal) in the
    database. Best-fitness genomes that improve on current policies
    become proposals with source="evolution".

    Parameters
    ----------
    db : AsyncSession
        Active database session (caller manages transaction).
    """
    if _session_factory is None:
        logger.warning("Session factory not initialized, skipping evolution loop")
        return

    start = time.perf_counter()
    total_proposals = 0

    try:
        # Get all active teams
        from orchestrator.db.models import Team
        teams_q = select(Team.id).where(Team.deleted_at.is_(None))
        team_ids = (await db.execute(teams_q)).scalars().all()

        if not team_ids:
            logger.debug("No active teams found for evolution")
            return

        for team_id in team_ids:
            tid = str(team_id)

            try:
                proposals = await _evolve_for_team(db, tid)
                total_proposals += proposals
            except Exception as exc:
                logger.error(
                    "Evolution failed for team %s: %s", tid, exc, exc_info=True
                )

        await db.commit()

        elapsed = time.perf_counter() - start
        logger.info(
            "Evolution loop complete: teams=%d proposals=%d elapsed=%dms",
            len(team_ids), total_proposals, round(elapsed * 1000),
        )

    except Exception as exc:
        logger.error("Evolution loop failed: %s", exc, exc_info=True)


async def _evolve_for_team(db: AsyncSession, team_id: str) -> int:
    """
    Run evolution for a single team. Returns number of proposals created.
    """
    # Fetch usage data
    aggregates = await _fetch_usage_aggregates(db, team_id)
    if len(aggregates) < MIN_AGGREGATES_FOR_EVOLUTION:
        logger.debug(
            "Insufficient aggregates for team %s (%d < %d), skipping",
            team_id, len(aggregates), MIN_AGGREGATES_FOR_EVOLUTION,
        )
        return 0

    # Fetch current policies
    current_yaml = await _fetch_current_policies_yaml(db, team_id)
    if not current_yaml:
        logger.debug("No active policies for team %s, skipping evolution", team_id)
        return 0

    # Run evolution
    evolver = ConstitutionalEvolver(
        population_size=DEFAULT_POPULATION_SIZE,
        generations=DEFAULT_GENERATIONS,
        tier="auto",
        seed=hash(team_id) & 0x7FFFFFFF,  # Deterministic per team
    )

    result = evolver.evolve(current_yaml, aggregates, team_id)

    if result.generations_run == 0:
        return 0

    # Evaluate current fitness for comparison
    current_genome = PolicyGenome.from_yaml(current_yaml)
    sim = GridWorldSimulator(
        agent_count=DEFAULT_AGENT_COUNT,
        max_steps=DEFAULT_SIM_STEPS,
        rng=random.Random(hash(team_id) & 0x7FFFFFFF),
    )
    sim.load_usage_patterns(aggregates)
    current_result = sim.simulate(current_genome)
    current_genome.fitness = current_result.fitness

    # Store generation record
    best_yaml = result.best_genome.to_yaml()
    mutations_desc = _describe_mutations(current_genome, result.best_genome)

    generation = EvolutionGeneration(
        team_id=team_id,
        generation_number=result.generations_run,
        population_size=result.population_size,
        best_fitness=result.best_fitness,
        avg_fitness=result.avg_fitness,
        best_genome_yaml=best_yaml,
        mutations_applied=json.dumps(mutations_desc),
        simulation_stats_json=json.dumps({
            "agent_count": DEFAULT_AGENT_COUNT,
            "steps": DEFAULT_SIM_STEPS,
            "cost_savings": round(result.best_fitness - current_genome.fitness, 6),
            "aggregates_used": len(aggregates),
        }),
        prover_results_json=(
            json.dumps(result.prover_result) if result.prover_result else None
        ),
        elapsed_ms=result.elapsed_ms,
    )
    db.add(generation)
    await db.flush()  # Get the generation ID

    # Create proposal if fitness improved
    proposals_created = 0
    improvement = result.best_fitness - current_genome.fitness
    improvement_pct = (
        (improvement / abs(current_genome.fitness) * 100)
        if current_genome.fitness != 0 else 0
    )

    if improvement > 0 and improvement_pct > 5.0:
        # Meaningful improvement — create a proposal
        proposal = EvolutionProposal(
            generation_id=generation.id,
            team_id=team_id,
            proposal_yaml=best_yaml,
            fitness_score=result.best_fitness,
            constitutional_diff=result.constitutional_diff,
            rationale=(
                f"Constitutional evolution found policies with "
                f"{improvement_pct:.1f}% better fitness "
                f"({current_genome.fitness:.4f} -> {result.best_fitness:.4f}). "
                f"Evolved over {result.generations_run} generations with "
                f"{result.population_size} candidates per generation. "
                f"Simulated against {len(aggregates)} usage patterns."
            ),
            status="pending",
        )
        db.add(proposal)
        await db.flush()  # Get proposal ID for CoT linkage
        proposals_created = 1

        logger.info(
            "Evolution proposal created for team %s: fitness %.4f -> %.4f (+%.1f%%)",
            team_id, current_genome.fitness, result.best_fitness, improvement_pct,
        )

        # ── CoT: successful evolution proposal ──────────────────────────
        if _cot_available:
            try:
                gene_count = result.best_genome.gene_count()
                cot_entry = await _cot_append(
                    db=db,
                    team_id=team_id,
                    decision_type="evolution_proposal",
                    trigger="evolution_cycle",
                    decision_summary=(
                        f"Evolution cycle produced policy proposal "
                        f"(fitness +{improvement_pct:.1f}%)"
                    ),
                    evidence_snapshot={
                        "aggregates_analyzed": len(aggregates),
                        "seed_fitness": round(current_genome.fitness, 4),
                        "best_fitness": round(result.best_fitness, 4),
                        "improvement_pct": round(improvement_pct, 2),
                        "generations_run": result.generations_run,
                        "population_size": result.population_size,
                    },
                    reasoning_steps=[
                        _cot_step(
                            "evolution",
                            f"Parsed {gene_count} genes from current policy genome",
                        ),
                        _cot_step(
                            "evolution",
                            f"Initialized population of {result.population_size} candidates",
                        ),
                        _cot_step(
                            "evolution",
                            f"Ran {result.generations_run} generations of optimization",
                        ),
                        _cot_step(
                            "evolution",
                            f"Best fitness improved from {current_genome.fitness:.4f} "
                            f"to {result.best_fitness:.4f} (+{improvement_pct:.1f}%)",
                            "minimum improvement: 5%",
                            "Proposal created",
                        ),
                    ],
                    alternatives_considered=[
                        {"genome_rank": 2, "fitness": round(result.avg_fitness, 4)},
                    ],
                    linked_evolution_gen_id=str(generation.id),
                    regulatory_tags=_cot_tags("evolution_proposal"),
                )
                # Link CoT entry to the proposal
                if cot_entry and cot_entry.get("id") and hasattr(proposal, "id") and proposal.id:
                    await _cot_link(db, cot_entry["id"], str(proposal.id))
            except Exception:
                logger.debug("CoT emission failed for evolution proposal", exc_info=True)

    else:
        logger.debug(
            "Evolution did not improve for team %s: current=%.4f best=%.4f",
            team_id, current_genome.fitness, result.best_fitness,
        )

        # ── CoT: no improvement ─────────────────────────────────────────
        if _cot_available:
            try:
                gene_count = result.best_genome.gene_count()
                await _cot_append(
                    db=db,
                    team_id=team_id,
                    decision_type="evolution_proposal",
                    trigger="evolution_cycle",
                    decision_summary=(
                        f"Evolution cycle completed — no improvement found "
                        f"({improvement_pct:.1f}%)"
                    ),
                    evidence_snapshot={
                        "aggregates_analyzed": len(aggregates),
                        "seed_fitness": round(current_genome.fitness, 4),
                        "best_fitness": round(result.best_fitness, 4),
                        "improvement_pct": round(improvement_pct, 2),
                        "generations_run": result.generations_run,
                        "population_size": result.population_size,
                    },
                    reasoning_steps=[
                        _cot_step(
                            "evolution",
                            f"Parsed {gene_count} genes from current policy genome",
                        ),
                        _cot_step(
                            "evolution",
                            f"Initialized population of {result.population_size} candidates",
                        ),
                        _cot_step(
                            "evolution",
                            f"Ran {result.generations_run} generations of optimization",
                        ),
                        _cot_step(
                            "evolution",
                            f"Best fitness {result.best_fitness:.4f} did not exceed "
                            f"seed {current_genome.fitness:.4f} by >5%",
                            "minimum improvement: 5%",
                            "No proposal created",
                        ),
                    ],
                    linked_evolution_gen_id=str(generation.id),
                    regulatory_tags=_cot_tags("evolution_proposal"),
                )
            except Exception:
                logger.debug("CoT emission failed for no-improvement case", exc_info=True)

    return proposals_created


def _describe_mutations(
    original: PolicyGenome,
    evolved: PolicyGenome,
) -> List[str]:
    """
    Generate human-readable descriptions of the mutations between
    the original and evolved genomes.
    """
    descriptions: List[str] = []
    orig_map = {g.name: g for g in original.genes}
    evol_map = {g.name: g for g in evolved.genes}

    for name, eg in evol_map.items():
        og = orig_map.get(name)
        if og is None:
            descriptions.append(f"Added gene: {name} = {eg.value()!r}")
        elif og.value() != eg.value():
            descriptions.append(
                f"Mutated {name}: {og.value()!r} -> {eg.value()!r}"
            )

    for name in orig_map:
        if name not in evol_map:
            descriptions.append(f"Removed gene: {name}")

    return descriptions
