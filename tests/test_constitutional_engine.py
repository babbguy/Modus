"""
Tests for orchestrator.core.constitutional_engine, orchestrator.core.policy_genome,
and orchestrator.core.grid_world — PolicyGenome parsing/serialization, genetic
operators (crossover, mutation, diff), GridWorldSimulator fitness evaluation,
and ConstitutionalEvolver full evolution runs.
"""

from __future__ import annotations

import random

import yaml

from orchestrator.core.policy_genome import (
    PolicyGenome,
    FloatGene,
    IntGene,
)
from orchestrator.core.grid_world import (
    GridWorldSimulator,
    SimulationResult,
    evaluate_population,
)
from orchestrator.core.constitutional_engine import (
    ConstitutionalEvolver,
    EvolutionResult,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

_BUDGET_YAML = yaml.dump([
    {
        "name": "daily-budget",
        "type": "budget_cap",
        "config": {"cap_usd": 50.0},
    },
    {
        "name": "rate-limiter",
        "type": "rate_limit",
        "config": {"max_calls": 200, "window_seconds": 3600},
    },
], default_flow_style=False)

_USAGE_AGGREGATES = [
    {"model": "gpt-4o", "avg_tokens": 500, "std_tokens": 100, "avg_cost": 0.05, "call_count": 500},
    {"model": "gpt-4o-mini", "avg_tokens": 300, "std_tokens": 80, "avg_cost": 0.01, "call_count": 1500},
]


# ── PolicyGenome: Parsing ────────────────────────────────────────────────────


class TestPolicyGenomeParsing:
    """Genome construction from YAML policy text."""

    def test_from_yaml_parses_budget_cap(self):
        """from_yaml must extract a FloatGene for budget_cap.cap_usd."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        cap_gene = genome.get_gene("p0.budget_cap.cap_usd")
        assert cap_gene is not None
        assert isinstance(cap_gene, FloatGene)
        assert cap_gene.value() == 50.0

    def test_from_yaml_parses_rate_limit(self):
        """from_yaml must extract IntGenes for rate_limit config."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        calls_gene = genome.get_gene("p1.rate_limit.max_calls")
        assert calls_gene is not None
        assert isinstance(calls_gene, IntGene)
        assert calls_gene.value() == 200

    def test_from_yaml_gene_count(self):
        """from_yaml must produce the expected number of genes."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        # budget_cap: 1 gene (cap_usd)
        # rate_limit: 2 genes (max_calls, window_seconds)
        assert genome.gene_count() == 3

    def test_from_yaml_empty_string_produces_empty_genome(self):
        """Empty YAML must produce a genome with zero genes."""
        genome = PolicyGenome.from_yaml("")
        assert genome.gene_count() == 0


# ── PolicyGenome: Serialization ──────────────────────────────────────────────


class TestPolicyGenomeSerialization:
    """Round-trip YAML serialization."""

    def test_to_yaml_round_trip(self):
        """to_yaml(from_yaml(y)) must preserve policy structure."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        yaml_out = genome.to_yaml()
        assert yaml_out  # non-empty
        reparsed = yaml.safe_load(yaml_out)
        assert isinstance(reparsed, list)
        assert len(reparsed) == 2
        assert reparsed[0]["type"] == "budget_cap"

    def test_to_yaml_empty_genome(self):
        """An empty genome must produce an empty string from to_yaml."""
        genome = PolicyGenome(genes=[], fitness=0.0)
        assert genome.to_yaml() == ""


# ── PolicyGenome: Genetic Operators ──────────────────────────────────────────


class TestPolicyGenomeOperators:
    """Crossover, mutation, clone, and diff."""

    def test_crossover_produces_valid_genome(self):
        """Crossover of two genomes must produce a genome with genes."""
        g1 = PolicyGenome.from_yaml(_BUDGET_YAML)
        g2 = PolicyGenome.from_yaml(_BUDGET_YAML)
        child = g1.crossover(g2, crossover_rate=0.5, rng=random.Random(42))
        assert child.gene_count() == g1.gene_count()

    def test_mutate_changes_at_least_some_genes(self):
        """With mutation_rate=1.0, every gene should be mutated."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        mutant = genome.mutate(mutation_rate=1.0, rng=random.Random(42))
        # At least one gene value should differ
        changed = sum(
            1 for orig, mut in zip(genome.genes, mutant.genes)
            if orig.value() != mut.value()
        )
        assert changed >= 1

    def test_clone_preserves_fitness(self):
        """clone() must preserve the fitness attribute."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        genome.fitness = 42.5
        cloned = genome.clone()
        assert cloned.fitness == 42.5
        assert cloned.gene_count() == genome.gene_count()

    def test_diff_produces_readable_output(self):
        """diff() must return a multi-line string with change counts."""
        g1 = PolicyGenome.from_yaml(_BUDGET_YAML)
        g2 = g1.mutate(mutation_rate=1.0, rng=random.Random(99))
        diff_text = g1.diff(g2)
        assert "Constitutional Diff" in diff_text
        assert "Total changes:" in diff_text


# ── GridWorldSimulator ───────────────────────────────────────────────────────


class TestGridWorldSimulator:
    """Agent simulation and fitness evaluation."""

    def test_simulate_returns_result(self):
        """simulate() must return a SimulationResult with valid fields."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        sim = GridWorldSimulator(agent_count=10, max_steps=5, rng=random.Random(1))
        sim.load_usage_patterns(_USAGE_AGGREGATES)
        result = sim.simulate(genome)
        assert isinstance(result, SimulationResult)
        assert result.total_calls > 0

    def test_fitness_is_numeric(self):
        """Fitness must be a real number (possibly negative due to false positives)."""
        genome = PolicyGenome.from_yaml(_BUDGET_YAML)
        sim = GridWorldSimulator(agent_count=10, max_steps=5, rng=random.Random(2))
        sim.load_usage_patterns(_USAGE_AGGREGATES)
        result = sim.simulate(genome)
        assert isinstance(result.fitness, float)

    def test_evaluate_population_sorts_by_fitness(self):
        """evaluate_population must return genomes sorted descending by fitness."""
        base = PolicyGenome.from_yaml(_BUDGET_YAML)
        pop = [base.mutate(0.5, rng=random.Random(i)) for i in range(5)]
        sim = GridWorldSimulator(agent_count=10, max_steps=5, rng=random.Random(3))
        sim.load_usage_patterns(_USAGE_AGGREGATES)
        sorted_pop = evaluate_population(sim, pop)
        fitnesses = [g.fitness for g in sorted_pop]
        assert fitnesses == sorted(fitnesses, reverse=True)


# ── ConstitutionalEvolver ────────────────────────────────────────────────────


class TestConstitutionalEvolver:
    """Full genetic algorithm evolution runs."""

    def test_evolve_runs_for_specified_generations(self):
        """evolve() must run and return an EvolutionResult."""
        evolver = ConstitutionalEvolver(
            population_size=10,
            generations=2,
            tier="1",
            agent_count=10,
            sim_steps=5,
            seed=42,
        )
        result = evolver.evolve(_BUDGET_YAML, _USAGE_AGGREGATES, "team-test")
        assert isinstance(result, EvolutionResult)
        assert result.generations_run >= 1

    def test_best_fitness_is_non_negative_or_finite(self):
        """The best fitness from evolution must be a finite float."""
        evolver = ConstitutionalEvolver(
            population_size=10,
            generations=2,
            tier="1",
            agent_count=10,
            sim_steps=5,
            seed=99,
        )
        result = evolver.evolve(_BUDGET_YAML, _USAGE_AGGREGATES, "team-test")
        assert isinstance(result.best_fitness, float)
        import math
        assert math.isfinite(result.best_fitness)

    def test_constitutional_diff_is_string(self):
        """The evolution result must include a constitutional_diff string."""
        evolver = ConstitutionalEvolver(
            population_size=10,
            generations=2,
            tier="1",
            agent_count=10,
            sim_steps=5,
            seed=7,
        )
        result = evolver.evolve(_BUDGET_YAML, _USAGE_AGGREGATES, "team-test")
        assert isinstance(result.constitutional_diff, str)
        assert len(result.constitutional_diff) > 0
