"""
Modus Federator — Blind Aggregator
=========================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Aggregates encrypted delta METADATA without ever decrypting payloads.

The federator can ONLY see:
    - fitness_improvement (float)
    - gene_count (int)
    - generation_span (int)
    - industry_type (string)
    - epoch_week (string)

From these signals it computes:
    - Fitness trends (avg, median, percentiles)
    - Evolution activity (gene churn, generation depth)
    - Industry benchmarks (cross-sector comparisons)
    - Confidence scores (based on contributor count and consistency)

Stdlib only — uses statistics module.
"""
from __future__ import annotations

import logging
import statistics as stats
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from federator.db.models import EncryptedDelta, MergedResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AggregateResult:
    """Computed aggregate for an industry or cross-industry."""
    industry_type: str
    epoch_week: str
    fitness_trend: dict
    evolution_activity: dict
    participating_instances: int
    confidence_score: float


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear interpolation percentile on a sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = pct * (len(sorted_vals) - 1)
    lower = int(idx)
    upper = min(lower + 1, len(sorted_vals) - 1)
    frac = idx - lower
    return round(sorted_vals[lower] + frac * (sorted_vals[upper] - sorted_vals[lower]), 6)


def aggregate_metadata(
    fitness_improvements: list[float],
    gene_counts: list[int],
    generation_spans: list[int],
    nonces: list[str],
) -> dict:
    """
    Compute aggregate statistics from metadata signals.

    All inputs are parallel lists (same index = same delta).
    Returns a dict suitable for storing as MergedResult.aggregate_data.
    """
    if not fitness_improvements:
        return {
            "fitness_trend": {},
            "evolution_activity": {},
            "participating_instances": 0,
            "confidence_score": 0.0,
        }

    n = len(fitness_improvements)
    unique_contributors = len(set(nonces))

    # ── Fitness trend ─────────────────────────────────────────────────────
    sorted_fitness = sorted(fitness_improvements)
    avg_fitness = stats.mean(fitness_improvements)
    fitness_trend = {
        "avg_improvement": round(avg_fitness, 6),
        "median_improvement": round(stats.median(fitness_improvements), 6),
        "p25_improvement": _percentile(sorted_fitness, 0.25),
        "p75_improvement": _percentile(sorted_fitness, 0.75),
        "p90_improvement": _percentile(sorted_fitness, 0.90),
        "stddev": round(stats.stdev(fitness_improvements), 6) if n > 1 else 0.0,
        "trend_direction": "improving" if avg_fitness > 0 else "declining" if avg_fitness < 0 else "stable",
        "sample_size": n,
    }

    # ── Evolution activity ────────────────────────────────────────────────
    evolution_activity = {
        "avg_gene_count": round(stats.mean(gene_counts), 2),
        "avg_generation_span": round(stats.mean(generation_spans), 2),
        "median_gene_count": round(stats.median(gene_counts), 2),
        "median_generation_span": round(stats.median(generation_spans), 2),
        "active_contributors": unique_contributors,
    }

    # ── Confidence score ──────────────────────────────────────────────────
    # Based on: number of contributors, consistency of fitness improvements
    contributor_factor = min(1.0, unique_contributors / 10.0)  # saturates at 10

    consistency_factor = 0.5
    if n > 1 and abs(avg_fitness) > 1e-9:
        cv = stats.stdev(fitness_improvements) / abs(avg_fitness)
        consistency_factor = max(0.0, 1.0 - min(1.0, cv))

    confidence = round(0.6 * contributor_factor + 0.4 * consistency_factor, 4)

    return {
        "fitness_trend": fitness_trend,
        "evolution_activity": evolution_activity,
        "participating_instances": unique_contributors,
        "confidence_score": confidence,
    }


async def run_aggregation(db: AsyncSession, min_contributors: int = 3) -> list[AggregateResult]:
    """
    Run periodic aggregation over all stored deltas.

    Groups by (industry_type, epoch_week) and computes aggregate
    metadata statistics. Only produces results for groups with
    >= min_contributors distinct nonces (privacy threshold).

    Also produces a cross-industry "all" aggregate.

    Returns list of AggregateResult objects created.
    """
    # Get the current epoch week
    now = datetime.now(timezone.utc)
    current_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

    # Find all (industry, week) groups with enough contributors
    groups_query = (
        select(
            EncryptedDelta.industry_type,
            EncryptedDelta.epoch_week,
            func.count(distinct(EncryptedDelta.nonce)).label("n_contributors"),
        )
        .group_by(EncryptedDelta.industry_type, EncryptedDelta.epoch_week)
    )
    groups_result = await db.execute(groups_query)
    groups = groups_result.all()

    results: list[AggregateResult] = []
    all_fitness: list[float] = []
    all_genes: list[int] = []
    all_spans: list[int] = []
    all_nonces: list[str] = []

    # Get current max version
    max_version_result = await db.execute(
        select(func.coalesce(func.max(MergedResult.version), 0))
    )
    next_version = max_version_result.scalar() + 1

    for industry, epoch_week, n_contributors in groups:
        if n_contributors < min_contributors:
            logger.debug(
                "Skipping %s/%s — only %d contributors (need %d)",
                industry, epoch_week, n_contributors, min_contributors,
            )
            continue

        # Fetch all deltas for this group
        deltas_query = (
            select(EncryptedDelta)
            .where(
                EncryptedDelta.industry_type == industry,
                EncryptedDelta.epoch_week == epoch_week,
            )
        )
        deltas_result = await db.execute(deltas_query)
        deltas = deltas_result.scalars().all()

        fitness = [d.fitness_improvement for d in deltas]
        genes = [d.gene_count for d in deltas]
        spans = [d.generation_span for d in deltas]
        nonces = [d.nonce for d in deltas]

        agg = aggregate_metadata(fitness, genes, spans, nonces)

        # Store
        merged = MergedResult(
            version=next_version,
            industry_type=industry,
            aggregate_data=agg,
            participating_instances=agg["participating_instances"],
            confidence_score=agg["confidence_score"],
            epoch_week=epoch_week,
        )
        db.add(merged)

        result = AggregateResult(
            industry_type=industry,
            epoch_week=epoch_week,
            fitness_trend=agg["fitness_trend"],
            evolution_activity=agg["evolution_activity"],
            participating_instances=agg["participating_instances"],
            confidence_score=agg["confidence_score"],
        )
        results.append(result)

        # Accumulate for cross-industry
        all_fitness.extend(fitness)
        all_genes.extend(genes)
        all_spans.extend(spans)
        all_nonces.extend(nonces)

        next_version += 1

    # Cross-industry aggregate
    if len(set(all_nonces)) >= min_contributors and all_fitness:
        agg_all = aggregate_metadata(all_fitness, all_genes, all_spans, all_nonces)
        merged_all = MergedResult(
            version=next_version,
            industry_type="all",
            aggregate_data=agg_all,
            participating_instances=agg_all["participating_instances"],
            confidence_score=agg_all["confidence_score"],
            epoch_week=current_week,
        )
        db.add(merged_all)

        results.append(AggregateResult(
            industry_type="all",
            epoch_week=current_week,
            fitness_trend=agg_all["fitness_trend"],
            evolution_activity=agg_all["evolution_activity"],
            participating_instances=agg_all["participating_instances"],
            confidence_score=agg_all["confidence_score"],
        ))

    await db.flush()
    logger.info("Aggregation produced %d results", len(results))
    return results
