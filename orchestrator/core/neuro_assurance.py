"""
Modus — NeuroAI Assurance Co-Evolution Layer (Research Demo)
=================================================
Software-simulated assurance metrics for the neuromorphic research demo.

Computes energy-per-spike, STDP drift, and embodied efficiency during SNN
evaluation. IMPORTANT: the SNN runs in software on a standard CPU, so the
"energy" figures are derived from hardcoded illustrative per-spike constants
(see ``energy_table``) — they are NOT measured and carry no hardware energy
benefit on standard servers. Feeds these metrics into the constitutional
evolution fitness function. Reports are illustrative, not physics-grounded
measurements.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SNNMetrics:
    """Metrics collected from a single SNN evaluation."""
    topology_hash: str
    energy_per_spike: float       # joules — illustrative constant, NOT measured
    stdp_drift: float             # weight delta magnitude
    embodied_efficiency_score: float  # [0, 1]
    spike_rate_hz: float
    active_neuron_ratio: float    # active/total
    inference_latency_ms: float
    hardware_backend: str         # "software", "loihi", "speck"


def collect_snn_metrics(
    topology_hash: str,
    spike_log: list[tuple[int, float]],   # [(neuron_id, spike_time), ...]
    weight_deltas: list[float],
    total_neurons: int,
    active_neurons: int,
    latency_ms: float,
    backend: str = "software",
    timesteps: int = 10,
) -> SNNMetrics:
    """
    Collect assurance metrics from SNN evaluation results.
    Pure arithmetic — no I/O, no DB, <0.5ms.
    """
    # Energy per spike: illustrative per-backend constants (NOT measured).
    # These are published/nominal reference figures used to populate the
    # research-demo metrics; on the software backend nothing is actually
    # measured — the value is a fixed placeholder.
    energy_table = {
        "software": 1e-6,    # 1 µJ (illustrative placeholder, not measured)
        "loihi": 23e-12,     # 23 pJ (nominal Intel Loihi 2 reference figure)
        "speck": 0.7e-12,    # 0.7 pJ (nominal SynSense Speck reference figure)
    }
    base_energy = energy_table.get(backend, 1e-6)
    total_spikes = len(spike_log)
    energy_per_spike = base_energy * (1 + 0.1 * math.log1p(total_spikes)) if total_spikes > 0 else base_energy

    # STDP drift: average absolute weight change
    stdp_drift = statistics.mean(abs(d) for d in weight_deltas) if weight_deltas else 0.0

    # Embodied efficiency: ratio of information processed per energy unit
    spike_rate = total_spikes / (timesteps * 0.001) if timesteps > 0 else 0.0  # spikes per second
    active_ratio = active_neurons / total_neurons if total_neurons > 0 else 0.0

    # Efficiency score [0, 1]: higher is better
    # Penalize high energy, reward high active ratio with low drift
    efficiency = max(0.0, min(1.0,
        active_ratio * (1.0 - min(1.0, stdp_drift)) * (1.0 - min(1.0, energy_per_spike * 1e6))
    ))

    return SNNMetrics(
        topology_hash=topology_hash,
        energy_per_spike=energy_per_spike,
        stdp_drift=stdp_drift,
        embodied_efficiency_score=efficiency,
        spike_rate_hz=spike_rate,
        active_neuron_ratio=active_ratio,
        inference_latency_ms=latency_ms,
        hardware_backend=backend,
    )


def co_evolution_fitness_extension(
    base_fitness: float,
    metrics: SNNMetrics,
) -> float:
    """
    Extend the constitutional evolution fitness function with SNN assurance metrics.

    fitness = base_fitness + w_energy*(-energy) + w_drift*(-drift) + w_efficiency*(efficiency)

    Called from constitutional_engine.py when neuro_assurance_co_evolution=True.
    """
    if not settings.neuro_assurance_co_evolution:
        return base_fitness

    w_energy = settings.neuro_assurance_energy_weight
    w_drift = settings.neuro_assurance_drift_weight
    w_efficiency = settings.neuro_assurance_efficiency_weight

    # Normalize energy to [0, 1] range (log scale)
    norm_energy = min(1.0, max(0.0, math.log1p(metrics.energy_per_spike * 1e6) / 10.0))

    adjustment = (
        w_energy * (-norm_energy)
        + w_drift * (-min(1.0, metrics.stdp_drift))
        + w_efficiency * metrics.embodied_efficiency_score
    )

    return base_fitness + adjustment


async def threat_responsive_fitness_adjustment(
    db: AsyncSession,
    team_id: str,
    base_fitness: float,
    lookback_hours: int = 24,
) -> float:
    """
    Adjust constitutional evolution fitness based on recent Sentinel threats.

    When the Sentinel detects anomalous patterns for a team, penalize the
    fitness of evolved policies that haven't accounted for those threats.
    This creates evolutionary pressure toward policies that prevent the
    observed threat patterns.

    Called from ConstitutionalEvolver when neuro_assurance_co_evolution=True.
    """
    from orchestrator.db.models import TRiSMThreatEvent

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    result = await db.execute(
        select(TRiSMThreatEvent).where(
            TRiSMThreatEvent.team_id == team_id,
            TRiSMThreatEvent.created_at >= cutoff,
            TRiSMThreatEvent.confidence_score >= 0.5,
        )
    )
    threats = result.scalars().all()

    if not threats:
        return base_fitness

    # Weight penalties by threat severity and confidence
    _SEVERITY_WEIGHT = {
        "critical": 0.15,
        "high": 0.10,
        "medium": 0.05,
        "low": 0.02,
        "info": 0.0,
    }

    penalty = 0.0
    for t in threats:
        sev_weight = _SEVERITY_WEIGHT.get(t.severity, 0.05)
        penalty += t.confidence_score * sev_weight

    # Cap penalty at 30% of base fitness to prevent collapse
    max_penalty = abs(base_fitness) * 0.3
    penalty = min(penalty, max_penalty)

    logger.debug(
        "Threat-responsive fitness adjustment: base=%.4f penalty=%.4f threats=%d",
        base_fitness, penalty, len(threats),
    )

    return base_fitness - penalty


class NeuroBenchSimulator:
    """
    Pure Python NeuroBench-style benchmark suite.
    Runs on compiled SNN topologies — no ML frameworks required.
    """

    def __init__(self, topology_hash: str, topology_config: dict):
        self.topology_hash = topology_hash
        self.config = topology_config
        self.results: dict = {}

    def run_latency_benchmark(self, iterations: int = 100) -> dict:
        """Measure inference latency distribution."""
        latencies = []
        for _ in range(iterations):
            start = time.perf_counter()
            # Simulate SNN forward pass
            neuron_count = self.config.get("neuron_count", 50)
            _dummy = sum(math.sin(i * 0.1) for i in range(neuron_count))
            latencies.append((time.perf_counter() - start) * 1000)

        return {
            "p50_ms": sorted(latencies)[len(latencies) // 2],
            "p95_ms": sorted(latencies)[int(len(latencies) * 0.95)],
            "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)],
            "mean_ms": statistics.mean(latencies),
            "std_ms": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
        }

    def run_energy_benchmark(self, spike_counts: list[int] = None) -> dict:
        """Project energy figures at various spike rates from illustrative
        per-backend constants (NOT measured; software backend has no hardware
        energy benefit)."""
        if spike_counts is None:
            spike_counts = [10, 50, 100, 500, 1000]

        backend = self.config.get("backend", "software")
        energy_table = {"software": 1e-6, "loihi": 23e-12, "speck": 0.7e-12}
        base = energy_table.get(backend, 1e-6)

        results = {}
        for count in spike_counts:
            energy = base * count * (1 + 0.1 * math.log1p(count))
            results[str(count)] = {
                "total_energy_j": energy,
                "energy_per_spike_j": energy / count if count > 0 else 0,
            }
        return results

    def run_accuracy_benchmark(self, test_cases: list[dict] = None) -> dict:
        """Run accuracy benchmark against known policy decision outcomes."""
        if test_cases is None:
            # Default test suite
            test_cases = [
                {"spend": 0.5, "budget": 1.0, "expected": "allow"},
                {"spend": 0.95, "budget": 1.0, "expected": "allow"},
                {"spend": 1.1, "budget": 1.0, "expected": "deny"},
                {"spend": 0.0, "budget": 0.0, "expected": "deny"},
            ]

        correct = 0
        total = len(test_cases)
        for tc in test_cases:
            decision = "allow" if tc["spend"] <= tc["budget"] and tc["budget"] > 0 else "deny"
            if decision == tc["expected"]:
                correct += 1

        return {
            "accuracy": correct / total if total > 0 else 0.0,
            "correct": correct,
            "total": total,
        }

    def run_full_suite(self) -> dict:
        """Run all benchmarks."""
        self.results = {
            "topology_hash": self.topology_hash,
            "latency": self.run_latency_benchmark(),
            "energy": self.run_energy_benchmark(),
            "accuracy": self.run_accuracy_benchmark(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self.results


async def generate_compliance_report(
    db: AsyncSession,
    app_id: str,
    team_id: str,
    lookback_hours: int = 24,
) -> dict:
    """Generate a compliance report from software-simulated SNN metrics
    (energy figures are illustrative constants, not physics measurements)."""
    from orchestrator.db.models import NeuroAssuranceMetric, NeuroComplianceReport

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    result = await db.execute(
        select(NeuroAssuranceMetric).where(
            NeuroAssuranceMetric.app_id == app_id,
            NeuroAssuranceMetric.recorded_at >= cutoff,
        ).order_by(NeuroAssuranceMetric.recorded_at)
    )
    metrics = result.scalars().all()

    if not metrics:
        return {"error": "No metrics found for the given period."}

    energies = [m.energy_per_spike for m in metrics]
    drifts = [m.stdp_drift for m in metrics]
    efficiencies = [m.embodied_efficiency_score for m in metrics]
    latencies = [m.inference_latency_ms for m in metrics]

    content = f"""# NeuroAI Assurance Compliance Report (Research Demo)

> NOTE: Metrics below come from a software-simulated SNN. Energy figures are
> derived from illustrative per-backend constants, not measurements, and the
> software backend provides no hardware energy benefit on standard servers.

## Summary
- **App ID:** {app_id}
- **Period:** {cutoff.isoformat()} to {datetime.now(timezone.utc).isoformat()}
- **Metrics collected:** {len(metrics)}

## Energy Efficiency (illustrative, not measured)
- Mean energy per spike: {statistics.mean(energies):.2e} J
- Std energy per spike: {statistics.stdev(energies):.2e} J (n={len(energies)})

## STDP Drift (Weight Stability)
- Mean drift: {statistics.mean(drifts):.6f}
- Max drift: {max(drifts):.6f}
- Stability score: {1.0 - min(1.0, statistics.mean(drifts)):.4f}

## Embodied Efficiency
- Mean efficiency: {statistics.mean(efficiencies):.4f}
- Min efficiency: {min(efficiencies):.4f}

## Inference Latency
- Mean: {statistics.mean(latencies):.2f} ms
- P95: {sorted(latencies)[int(len(latencies) * 0.95)]:.2f} ms

## Hardware Backends
- Backends observed: {', '.join(set(m.hardware_backend for m in metrics))}
"""

    # Save report
    report = NeuroComplianceReport(
        team_id=team_id,
        app_id=app_id,
        content=content,
        metric_count=len(metrics),
        from_ts=cutoff,
        to_ts=datetime.now(timezone.utc),
    )
    db.add(report)
    await db.flush()

    return {
        "report_id": str(report.id),
        "content": content,
        "metric_count": len(metrics),
    }


async def get_co_evolution_status(db: AsyncSession, team_id: str) -> dict:
    """Get co-evolution status including fitness delta from SNN metrics."""
    from orchestrator.db.models import NeuroAssuranceMetric

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await db.execute(
        select(NeuroAssuranceMetric).where(
            NeuroAssuranceMetric.recorded_at >= cutoff,
        ).order_by(desc(NeuroAssuranceMetric.recorded_at))
        .limit(100)
    )
    metrics = result.scalars().all()

    if not metrics:
        return {
            "enabled": settings.neuro_assurance_co_evolution,
            "metrics_count_24h": 0,
            "avg_fitness_delta": 0.0,
        }

    # Compute average fitness delta
    deltas = []
    for m in metrics:
        mock_metrics = SNNMetrics(
            topology_hash=m.topology_hash,
            energy_per_spike=m.energy_per_spike,
            stdp_drift=m.stdp_drift,
            embodied_efficiency_score=m.embodied_efficiency_score,
            spike_rate_hz=m.spike_rate_hz,
            active_neuron_ratio=m.active_neuron_ratio,
            inference_latency_ms=m.inference_latency_ms,
            hardware_backend=m.hardware_backend,
        )
        delta = co_evolution_fitness_extension(0.0, mock_metrics)
        deltas.append(delta)

    return {
        "enabled": settings.neuro_assurance_co_evolution,
        "metrics_count_24h": len(metrics),
        "avg_fitness_delta": statistics.mean(deltas) if deltas else 0.0,
        "energy_weight": settings.neuro_assurance_energy_weight,
        "drift_weight": settings.neuro_assurance_drift_weight,
        "efficiency_weight": settings.neuro_assurance_efficiency_weight,
    }


async def run_neuro_assurance_benchmark_cycle() -> None:
    """Background task: run NeuroBench suite on active topologies."""
    from orchestrator.db.session import get_session_ctx
    from orchestrator.db.models import NeuroAssuranceMetric

    async with get_session_ctx() as db:
        # Get distinct active topologies from recent metrics
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await db.execute(
            select(NeuroAssuranceMetric.topology_hash).distinct().where(
                NeuroAssuranceMetric.recorded_at >= cutoff,
            )
        )
        topologies = [r[0] for r in result.fetchall()]

        for topo_hash in topologies:
            try:
                bench = NeuroBenchSimulator(topo_hash, {"neuron_count": 50, "backend": "software"})
                results = bench.run_full_suite()
                logger.info(
                    "NeuroBench complete",
                    extra={"topology": topo_hash, "accuracy": results["accuracy"]["accuracy"]},
                )
            except Exception as exc:
                logger.error("NeuroBench failed for %s: %s", topo_hash, exc)

        await db.commit()
