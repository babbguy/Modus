"""
Constitutional Engine coverage.

Targets orchestrator.core.constitutional_engine: EvolutionResult,
ConstitutionalEvolver, tournament selection, population init,
next generation creation.
"""
from __future__ import annotations

import random

# ═══════════════════════════════════════════════════════════════════════════════
# 1. EVOLUTION RESULT
# ═══════════════════════════════════════════════════════════════════════════════

def test_evolution_result_to_dict():
    from orchestrator.core.constitutional_engine import EvolutionResult
    from orchestrator.core.policy_genome import PolicyGenome
    genome = PolicyGenome(genes=[])
    result = EvolutionResult(
        best_genome=genome,
        best_fitness=0.95,
        avg_fitness=0.75,
        generations_run=10,
        population_size=100,
        constitutional_diff="Changed budget cap",
        prover_result=None,
        elapsed_ms=1234,
    )
    d = result.to_dict()
    assert d["best_fitness"] == 0.95
    assert d["avg_fitness"] == 0.75
    assert d["generations_run"] == 10
    assert d["elapsed_ms"] == 1234
    assert d["constitutional_diff"] == "Changed budget cap"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. TOURNAMENT SELECTION
# ═══════════════════════════════════════════════════════════════════════════════

def test_tournament_select():
    from orchestrator.core.constitutional_engine import _tournament_select
    from orchestrator.core.policy_genome import PolicyGenome
    population = []
    for i in range(10):
        g = PolicyGenome(genes=[])
        g.fitness = float(i)
        population.append(g)
    rng = random.Random(42)
    selected = _tournament_select(population, 3, rng)
    assert selected.fitness >= 0  # Should return a valid genome


def test_tournament_select_small_population():
    from orchestrator.core.constitutional_engine import _tournament_select
    from orchestrator.core.policy_genome import PolicyGenome
    g1 = PolicyGenome(genes=[])
    g1.fitness = 5.0
    g2 = PolicyGenome(genes=[])
    g2.fitness = 10.0
    population = [g1, g2]
    rng = random.Random(42)
    selected = _tournament_select(population, 5, rng)
    assert selected.fitness == 10.0  # Best of entire population


# ═══════════════════════════════════════════════════════════════════════════════
# 3. CONSTITUTIONAL EVOLVER INIT
# ═══════════════════════════════════════════════════════════════════════════════

def test_evolver_init_defaults():
    from orchestrator.core.constitutional_engine import ConstitutionalEvolver
    e = ConstitutionalEvolver()
    assert e.population_size == 100
    assert e.generations == 10
    assert e.crossover_rate == 0.7
    assert e.mutation_rate == 0.1


def test_evolver_init_clamped():
    from orchestrator.core.constitutional_engine import ConstitutionalEvolver
    e = ConstitutionalEvolver(population_size=5, generations=100)
    assert e.population_size == 10  # min 10
    assert e.generations == 50  # max 50


# ═══════════════════════════════════════════════════════════════════════════════
# 4. POPULATION INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_init_population():
    from orchestrator.core.constitutional_engine import ConstitutionalEvolver
    from orchestrator.core.policy_genome import PolicyGenome
    e = ConstitutionalEvolver(population_size=20, seed=42)
    seed = PolicyGenome.from_yaml("""
- name: test-policy
  type: budget_cap
  config:
    cap_usd: "100"
    period: daily
""")
    pop = e._init_population(seed)
    assert len(pop) == 20
    # First should be a clone of seed
    assert pop[0].gene_count() == seed.gene_count()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. NEXT GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_create_next_generation():
    from orchestrator.core.constitutional_engine import ConstitutionalEvolver
    from orchestrator.core.policy_genome import PolicyGenome
    e = ConstitutionalEvolver(population_size=20, seed=42)
    # Create a population with fitness values
    pop = []
    for i in range(20):
        g = PolicyGenome.from_yaml(f"""
- name: policy-{i}
  type: budget_cap
  config:
    cap_usd: "{100 + i * 10}"
    period: daily
""")
        g.fitness = float(i) / 20.0
        pop.append(g)
    # Sort by fitness descending (as the evolver expects)
    pop.sort(key=lambda g: g.fitness, reverse=True)
    next_gen = e._create_next_generation(pop)
    assert len(next_gen) == 20


# ═══════════════════════════════════════════════════════════════════════════════
# 6. EVOLVE WITH EMPTY GENOME
# ═══════════════════════════════════════════════════════════════════════════════

def test_evolve_empty_genome():
    from orchestrator.core.constitutional_engine import ConstitutionalEvolver
    e = ConstitutionalEvolver(population_size=10, generations=2, seed=42)
    result = e.evolve(
        current_policies_yaml="",
        usage_aggregates=[],
        team_id="test-team",
    )
    assert result.generations_run == 0
    assert result.best_fitness == 0.0
    assert "No genes" in result.constitutional_diff


# ═══════════════════════════════════════════════════════════════════════════════
# 7. EVOLVE WITH POLICIES
# ═══════════════════════════════════════════════════════════════════════════════

def test_evolve_with_policies():
    from orchestrator.core.constitutional_engine import ConstitutionalEvolver
    e = ConstitutionalEvolver(
        population_size=10,
        generations=2,
        seed=42,
        agent_count=10,
        sim_steps=5,
    )
    yaml_text = """
- name: test-cap
  type: budget_cap
  config:
    cap_usd: "500"
    period: daily
"""
    usage_data = [
        {"model": "gpt-4o", "avg_input_tokens": 100, "avg_output_tokens": 50,
         "call_count": 1000, "total_cost": 50.0},
    ]
    result = e.evolve(
        current_policies_yaml=yaml_text,
        usage_aggregates=usage_data,
        team_id="test-team",
    )
    assert result.generations_run >= 1
    assert result.best_fitness >= 0.0
    assert result.elapsed_ms >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# 8. PROVER INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_run_prover_not_available():
    from orchestrator.core.constitutional_engine import ConstitutionalEvolver
    from orchestrator.core.policy_genome import PolicyGenome
    e = ConstitutionalEvolver(tier="1")
    genome = PolicyGenome(genes=[])
    result = e._run_prover(genome)
    assert result is None


def test_run_prover_tier2_warning():
    from orchestrator.core import constitutional_engine as mod
    original = mod._prover_available
    try:
        mod._prover_available = False
        e = mod.ConstitutionalEvolver(tier="2")
        from orchestrator.core.policy_genome import PolicyGenome
        result = e._run_prover(PolicyGenome(genes=[]))
        assert result is None
    finally:
        mod._prover_available = original


# ═══════════════════════════════════════════════════════════════════════════════
# 9. CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

def test_constants():
    from orchestrator.core.constitutional_engine import (
        DEFAULT_POPULATION_SIZE,
        DEFAULT_GENERATIONS,
        DEFAULT_CROSSOVER_RATE,
        DEFAULT_MUTATION_RATE,
        EVOLUTION_LOOKBACK_HOURS,
        MIN_AGGREGATES_FOR_EVOLUTION,
    )
    assert DEFAULT_POPULATION_SIZE == 100
    assert DEFAULT_GENERATIONS == 10
    assert DEFAULT_CROSSOVER_RATE == 0.7
    assert DEFAULT_MUTATION_RATE == 0.1
    assert EVOLUTION_LOOKBACK_HOURS == 168
    assert MIN_AGGREGATES_FOR_EVOLUTION == 10
