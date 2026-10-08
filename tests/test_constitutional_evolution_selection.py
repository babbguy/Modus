"""Tests for orchestrator.core.constitutional_engine — evolution, selection."""
from unittest.mock import MagicMock, patch
import random


from orchestrator.core.constitutional_engine import (
    ConstitutionalEvolver,
    EvolutionResult,
    _tournament_select,
    DEFAULT_POPULATION_SIZE,
    DEFAULT_GENERATIONS,
    DEFAULT_CROSSOVER_RATE,
    DEFAULT_MUTATION_RATE,
    DEFAULT_TOURNAMENT_SIZE,
    DEFAULT_ELITISM_RATIO,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_genome(fitness=0.5):
    """Create a mock PolicyGenome with a given fitness."""
    g = MagicMock()
    g.fitness = fitness
    g.gene_count.return_value = 3
    g.clone.return_value = MagicMock(fitness=fitness, gene_count=MagicMock(return_value=3))
    g.mutate.return_value = MagicMock(fitness=0.0, gene_count=MagicMock(return_value=3))
    g.crossover.return_value = MagicMock(fitness=0.0, gene_count=MagicMock(return_value=3))
    g.diff.return_value = "no changes"
    g.to_yaml.return_value = "- name: test\n  type: budget_cap"
    return g


# ── _tournament_select ───────────────────────────────────────────────────────

class TestTournamentSelect:
    def test_selects_best(self):
        rng = random.Random(42)
        pop = [_make_genome(f) for f in [0.1, 0.9, 0.5]]
        # With tournament_size >= population size, always picks best
        best = _tournament_select(pop, 10, rng)
        assert best.fitness == 0.9

    def test_small_population(self):
        rng = random.Random(42)
        pop = [_make_genome(0.7)]
        best = _tournament_select(pop, 3, rng)
        assert best.fitness == 0.7

    def test_equal_fitness(self):
        rng = random.Random(42)
        pop = [_make_genome(0.5) for _ in range(5)]
        best = _tournament_select(pop, 3, rng)
        assert best.fitness == 0.5


# ── EvolutionResult ──────────────────────────────────────────────────────────

class TestEvolutionResult:
    def test_to_dict(self):
        result = EvolutionResult(
            best_genome=MagicMock(),
            best_fitness=0.85,
            avg_fitness=0.65,
            generations_run=10,
            population_size=100,
            constitutional_diff="added budget rule",
            prover_result=None,
            elapsed_ms=1234,
        )
        d = result.to_dict()
        assert d["best_fitness"] == 0.85
        assert d["avg_fitness"] == 0.65
        assert d["generations_run"] == 10
        assert d["population_size"] == 100
        assert d["elapsed_ms"] == 1234
        assert d["prover_result"] is None

    def test_to_dict_with_prover(self):
        result = EvolutionResult(
            best_genome=MagicMock(),
            best_fitness=0.9,
            avg_fitness=0.7,
            generations_run=5,
            population_size=50,
            constitutional_diff="diff",
            prover_result={"proven": True},
            elapsed_ms=500,
        )
        d = result.to_dict()
        assert d["prover_result"] == {"proven": True}


# ── ConstitutionalEvolver ────────────────────────────────────────────────────

class TestConstitutionalEvolver:
    def test_init_defaults(self):
        evolver = ConstitutionalEvolver()
        assert evolver.population_size == DEFAULT_POPULATION_SIZE
        assert evolver.generations == DEFAULT_GENERATIONS
        assert evolver.crossover_rate == DEFAULT_CROSSOVER_RATE
        assert evolver.mutation_rate == DEFAULT_MUTATION_RATE

    def test_init_clamps_population(self):
        evolver = ConstitutionalEvolver(population_size=5)
        assert evolver.population_size == 10  # min 10

        evolver = ConstitutionalEvolver(population_size=1000)
        assert evolver.population_size == 500  # max 500

    def test_init_clamps_generations(self):
        evolver = ConstitutionalEvolver(generations=0)
        assert evolver.generations == 1

        evolver = ConstitutionalEvolver(generations=100)
        assert evolver.generations == 50

    def test_init_clamps_tournament(self):
        evolver = ConstitutionalEvolver(tournament_size=1)
        assert evolver.tournament_size == 2  # min 2

    def test_init_clamps_elitism(self):
        evolver = ConstitutionalEvolver(elitism_ratio=-0.5)
        assert evolver.elitism_ratio == 0.0

        evolver = ConstitutionalEvolver(elitism_ratio=1.0)
        assert evolver.elitism_ratio == 0.5

    def test_seed_reproducibility(self):
        e1 = ConstitutionalEvolver(seed=42)
        e2 = ConstitutionalEvolver(seed=42)
        # Same seed should produce same RNG state
        assert e1.rng.random() == e2.rng.random()

    @patch("orchestrator.core.constitutional_engine.PolicyGenome")
    @patch("orchestrator.core.constitutional_engine.GridWorldSimulator")
    @patch("orchestrator.core.constitutional_engine.evaluate_population")
    def test_evolve_no_genes(self, mock_eval, mock_sim_cls, mock_genome_cls):
        """Evolve with empty genome returns early."""
        seed = MagicMock()
        seed.gene_count.return_value = 0
        mock_genome_cls.from_yaml.return_value = seed

        evolver = ConstitutionalEvolver(population_size=10, generations=2, seed=1)
        result = evolver.evolve("", [], "team1")
        assert result.generations_run == 0
        assert result.constitutional_diff == "No genes to evolve."

    def test_init_population(self):
        evolver = ConstitutionalEvolver(population_size=20, seed=1)
        seed = _make_genome(0.5)
        seed.mutate.return_value = _make_genome(0.0)
        pop = evolver._init_population(seed)
        assert len(pop) == 20

    def test_create_next_generation(self):
        evolver = ConstitutionalEvolver(
            population_size=10,
            elitism_ratio=0.2,
            seed=1,
        )
        pop = [_make_genome(f) for f in [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05]]
        for g in pop:
            g.crossover.return_value = _make_genome(0.0)
            g.crossover.return_value.mutate = MagicMock(return_value=_make_genome(0.0))

        next_gen = evolver._create_next_generation(pop)
        assert len(next_gen) == 10

    def test_run_prover_no_prover(self):
        evolver = ConstitutionalEvolver()
        with patch("orchestrator.core.constitutional_engine._prover_available", False):
            result = evolver._run_prover(_make_genome(0.9))
            assert result is None


# ── Constants ────────────────────────────────────────────────────────────────

class TestConstants:
    def test_defaults_reasonable(self):
        assert DEFAULT_POPULATION_SIZE == 100
        assert DEFAULT_GENERATIONS == 10
        assert 0 < DEFAULT_CROSSOVER_RATE < 1
        assert 0 < DEFAULT_MUTATION_RATE < 1
        assert DEFAULT_TOURNAMENT_SIZE >= 2
        assert 0 < DEFAULT_ELITISM_RATIO < 1
