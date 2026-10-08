"""
Tests for orchestrator.core.neuro_assurance —
SNN metrics collection, co-evolution fitness, and NeuroBench simulator.
"""
from __future__ import annotations


from orchestrator.core.neuro_assurance import (
    SNNMetrics,
    collect_snn_metrics,
    co_evolution_fitness_extension,
    NeuroBenchSimulator,
)


class TestCollectSNNMetrics:
    """SNN metrics collection from evaluation results."""

    def test_basic_collection(self):
        """Should return valid SNNMetrics from spike log."""
        metrics = collect_snn_metrics(
            topology_hash="abc123",
            spike_log=[(0, 0.1), (1, 0.2), (2, 0.3)],
            weight_deltas=[0.01, -0.02, 0.005],
            total_neurons=10,
            active_neurons=5,
            latency_ms=1.5,
            backend="software",
        )
        assert isinstance(metrics, SNNMetrics)
        assert metrics.topology_hash == "abc123"
        assert metrics.active_neuron_ratio == 0.5
        assert metrics.hardware_backend == "software"
        assert metrics.inference_latency_ms == 1.5

    def test_empty_spike_log(self):
        """Should handle empty spike log gracefully."""
        metrics = collect_snn_metrics(
            topology_hash="empty",
            spike_log=[],
            weight_deltas=[],
            total_neurons=10,
            active_neurons=0,
            latency_ms=0.1,
        )
        assert metrics.spike_rate_hz == 0.0
        assert metrics.active_neuron_ratio == 0.0

    def test_energy_per_spike_varies_by_backend(self):
        """Different backends should produce different energy values."""
        kwargs = dict(
            topology_hash="t", spike_log=[(0, 0.1)],
            weight_deltas=[0.01], total_neurons=10,
            active_neurons=5, latency_ms=1.0,
        )
        m_sw = collect_snn_metrics(**kwargs, backend="software")
        m_loihi = collect_snn_metrics(**kwargs, backend="loihi")
        m_speck = collect_snn_metrics(**kwargs, backend="speck")
        assert m_sw.energy_per_spike > m_loihi.energy_per_spike
        assert m_loihi.energy_per_spike > m_speck.energy_per_spike

    def test_efficiency_score_bounded(self):
        """Efficiency score must be in [0, 1]."""
        for _ in range(20):
            import random
            metrics = collect_snn_metrics(
                topology_hash="t",
                spike_log=[(i, i * 0.01) for i in range(random.randint(0, 100))],
                weight_deltas=[random.uniform(-1, 1) for _ in range(10)],
                total_neurons=max(1, random.randint(1, 100)),
                active_neurons=random.randint(0, 100),
                latency_ms=random.uniform(0.1, 10),
                backend=random.choice(["software", "loihi", "speck"]),
            )
            assert 0.0 <= metrics.embodied_efficiency_score <= 1.0


class TestCoEvolutionFitness:
    """Co-evolution fitness extension."""

    def test_disabled_returns_base(self, monkeypatch):
        """When co-evolution disabled, return base fitness unchanged."""
        monkeypatch.setattr(
            "orchestrator.core.neuro_assurance.settings.neuro_assurance_co_evolution", False
        )
        metrics = SNNMetrics(
            topology_hash="t", energy_per_spike=1e-6, stdp_drift=0.01,
            embodied_efficiency_score=0.8, spike_rate_hz=100,
            active_neuron_ratio=0.5, inference_latency_ms=1.0,
            hardware_backend="software",
        )
        assert co_evolution_fitness_extension(1.0, metrics) == 1.0

    def test_enabled_modifies_fitness(self, monkeypatch):
        """When enabled, fitness should be modified."""
        monkeypatch.setattr(
            "orchestrator.core.neuro_assurance.settings.neuro_assurance_co_evolution", True
        )
        monkeypatch.setattr(
            "orchestrator.core.neuro_assurance.settings.neuro_assurance_energy_weight", 0.1
        )
        monkeypatch.setattr(
            "orchestrator.core.neuro_assurance.settings.neuro_assurance_drift_weight", 0.1
        )
        monkeypatch.setattr(
            "orchestrator.core.neuro_assurance.settings.neuro_assurance_efficiency_weight", 0.05
        )
        metrics = SNNMetrics(
            topology_hash="t", energy_per_spike=1e-6, stdp_drift=0.01,
            embodied_efficiency_score=0.8, spike_rate_hz=100,
            active_neuron_ratio=0.5, inference_latency_ms=1.0,
            hardware_backend="software",
        )
        result = co_evolution_fitness_extension(1.0, metrics)
        assert result != 1.0


class TestNeuroBenchSimulator:
    """NeuroBench benchmark suite."""

    def test_latency_benchmark(self):
        bench = NeuroBenchSimulator("topo-1", {"neuron_count": 10, "backend": "software"})
        result = bench.run_latency_benchmark(iterations=10)
        assert "p50_ms" in result
        assert "p95_ms" in result
        assert result["p50_ms"] >= 0

    def test_energy_benchmark(self):
        bench = NeuroBenchSimulator("topo-1", {"neuron_count": 10, "backend": "software"})
        result = bench.run_energy_benchmark([10, 100])
        assert "10" in result
        assert "100" in result
        assert result["10"]["total_energy_j"] < result["100"]["total_energy_j"]

    def test_accuracy_benchmark(self):
        bench = NeuroBenchSimulator("topo-1", {"neuron_count": 10})
        result = bench.run_accuracy_benchmark()
        assert result["accuracy"] >= 0.0
        assert result["total"] > 0

    def test_full_suite(self):
        bench = NeuroBenchSimulator("topo-1", {"neuron_count": 10})
        result = bench.run_full_suite()
        assert "latency" in result
        assert "energy" in result
        assert "accuracy" in result
        assert result["topology_hash"] == "topo-1"
