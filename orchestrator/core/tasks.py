"""
Background task loops for the Modus Orchestrator.

Each function runs as a long-lived asyncio task, started from the lifespan
handler.  Lazy imports inside each function are intentional -- they keep
startup fast by deferring heavy module loads until the task actually runs.
"""

from __future__ import annotations

import asyncio
import logging

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)


# ── Core background tasks ─────────────────────────────────────────────────────

async def _run_aggregation_loop() -> None:
    """
    Periodically compute hourly and daily aggregates from raw usage_records,
    then evaluate thresholds against the freshly computed aggregates.
    Runs every settings.aggregate_interval_seconds.
    """
    from orchestrator.core.aggregator import run_aggregation_cycle
    from orchestrator.core.threshold_evaluator import evaluate_thresholds
    logger.info(
        "Aggregation task started",
        extra={"interval_seconds": settings.aggregate_interval_seconds},
    )
    while True:
        try:
            await asyncio.sleep(settings.aggregate_interval_seconds)
            await run_aggregation_cycle()
            # Evaluate thresholds against fresh aggregates
            await evaluate_thresholds()
        except asyncio.CancelledError:
            logger.info("Aggregation task stopping")
            break
        except Exception as exc:
            logger.error("Aggregation cycle failed", exc_info=exc)
            # Continue — don't let a bad aggregation cycle kill the task


async def _run_maintenance_loop() -> None:
    """
    Periodic maintenance: prune stale heartbeats and expired batch IDs.
    Runs every hour.
    """
    from orchestrator.core.maintenance import (
        prune_heartbeats, prune_batch_ids, prune_real_time_spend,
        auto_reset_budget_suspensions, ensure_partitions,
        compact_usage_records, compact_hourly_to_daily,
    )
    logger.info("Maintenance task started")
    while True:
        try:
            await asyncio.sleep(3600)  # 1 hour
            await prune_heartbeats()
            await prune_batch_ids()
            await prune_real_time_spend()
            await auto_reset_budget_suspensions()
            await ensure_partitions()
            # Storage compaction: raw → hourly → daily
            await compact_usage_records()
            await compact_hourly_to_daily()
        except asyncio.CancelledError:
            logger.info("Maintenance task stopping")
            break
        except Exception as exc:
            logger.error("Maintenance cycle failed", exc_info=exc)


async def _run_pricing_sync_loop() -> None:
    """
    Periodic pricing table sync from bundled pricing data.
    Runs every settings.pricing_sync_interval_hours.
    """
    from orchestrator.core.pricing_sync import sync_pricing
    interval = settings.pricing_sync_interval_hours * 3600
    logger.info(
        "Pricing sync task started",
        extra={"interval_hours": settings.pricing_sync_interval_hours},
    )
    # Run immediately on startup to ensure pricing table is populated
    try:
        await sync_pricing()
    except Exception as exc:
        logger.error("Initial pricing sync failed", exc_info=exc)

    while True:
        try:
            await asyncio.sleep(interval)
            await sync_pricing()
        except asyncio.CancelledError:
            logger.info("Pricing sync task stopping")
            break
        except Exception as exc:
            logger.error("Pricing sync failed", exc_info=exc)


async def _run_anomaly_loop() -> None:
    from orchestrator.core.insights_engine import (
        insights_task_loop, run_anomaly_scan,
    )
    await insights_task_loop(
        "task.anomaly_scan", run_anomaly_scan, default_interval_seconds=300
    )


async def _run_forecast_loop() -> None:
    from orchestrator.core.insights_engine import (
        insights_task_loop, run_forecast_update,
    )
    await insights_task_loop(
        "task.forecast", run_forecast_update, default_interval_seconds=21600
    )


async def _run_recommendations_loop() -> None:
    from orchestrator.core.insights_engine import (
        insights_task_loop, run_recommendation_refresh,
    )
    await insights_task_loop(
        "task.recommendations", run_recommendation_refresh, default_interval_seconds=86400
    )


async def _run_efficiency_audit_loop() -> None:
    from orchestrator.core.insights_engine import (
        insights_task_loop, run_prompt_efficiency_audit,
    )
    await insights_task_loop(
        "task.efficiency_audit", run_prompt_efficiency_audit,
        default_interval_seconds=86400  # once per day
    )


async def _run_governance_loop() -> None:
    """Autonomous governance: detect patterns, propose YAML policy diffs."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.governance_loop import run_governance_loop
    await insights_task_loop(
        "task.governance_loop", run_governance_loop,
        default_interval_seconds=3600,  # hourly
    )


async def _run_trajectory_fingerprint_loop() -> None:
    """Trajectory engine: compute session fingerprints for Monte-Carlo simulation."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.trajectory_engine import run_trajectory_fingerprint_update
    await insights_task_loop(
        "task.trajectory_fingerprints", run_trajectory_fingerprint_update,
        default_interval_seconds=3600,  # hourly
    )


async def _run_trism_pattern_sync_loop() -> None:
    """TRiSM: sync builtin patterns to DB and apply updates."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.trism_sentinel import sync_builtin_patterns
    await insights_task_loop(
        "task.trism_pattern_sync", sync_builtin_patterns,
        default_interval_seconds=86400,  # daily
    )


async def _run_evolution_loop_task() -> None:
    """Constitutional Evolution: evolve policies via genetic algorithm."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.constitutional_engine import run_evolution_loop
    await insights_task_loop(
        "task.constitutional_evolution", run_evolution_loop,
        default_interval_seconds=3600,  # hourly
    )


async def _run_neuromorphic_metrics_loop() -> None:
    """Neuromorphic: aggregate enforcement metrics."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.neuromorphic_engine import collect_neuromorphic_metrics
    await insights_task_loop(
        "task.neuromorphic_metrics", collect_neuromorphic_metrics,
        default_interval_seconds=300,  # every 5 minutes
    )


# ── Phase 9 background tasks ─────────────────────────────────────────────────

async def _run_neuro_assurance_loop() -> None:
    """NeuroAI Assurance: periodic NeuroBench benchmarking."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.neuro_assurance import run_neuro_assurance_benchmark_cycle
    await insights_task_loop(
        "task.neuro_assurance_benchmark", run_neuro_assurance_benchmark_cycle,
        default_interval_seconds=settings.neuro_assurance_benchmark_interval_seconds,
    )


async def _run_federation_sync_loop() -> None:
    """Federation: periodic sync with aggregator."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.federation_engine import run_federation_sync_cycle
    await insights_task_loop(
        "task.federation_sync", run_federation_sync_cycle,
        default_interval_seconds=settings.federation_sync_interval_hours * 3600,
    )


# ── Phase 10 background tasks ─────────────────────────────────────────────────

async def _run_pqc_assessment_loop() -> None:
    """PQC Assessment: periodic team readiness scoring."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.pqc_assessment import run_pqc_assessment_cycle
    await insights_task_loop(
        "task.pqc_assessment", run_pqc_assessment_cycle,
        default_interval_seconds=3600,
    )


async def _run_report_scheduler_loop() -> None:
    """Finance report scheduler: generate and deliver scheduled reports via webhook."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.report_scheduler import run_report_scheduler
    await insights_task_loop(
        "task.report_scheduler", run_report_scheduler,
        default_interval_seconds=300  # check every 5 minutes
    )


async def _run_routing_calibrator_loop() -> None:
    """Routing engine: promote fingerprints through observe -> calibrate -> route."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.routing_calibrator import run_calibrator_cycle
    await insights_task_loop(
        "task.routing_calibrator", run_calibrator_cycle,
        default_interval_seconds=settings.routing_calibrator_interval_seconds,
    )


async def _run_drift_monitor_loop() -> None:
    """Routing engine: detect input distribution drift for active fingerprints."""
    from orchestrator.core.insights_engine import insights_task_loop
    from orchestrator.core.drift_monitor import run_drift_monitor_cycle
    await insights_task_loop(
        "task.drift_monitor", run_drift_monitor_cycle,
        default_interval_seconds=settings.routing_drift_monitor_interval_seconds,
    )


async def _run_attribution_loop() -> None:
    """Process completed agent sessions for cost attribution (Phase 5)."""
    from orchestrator.core.attribution_engine import process_pending_sessions
    logger.info("Attribution engine task started")
    while True:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds
            await process_pending_sessions()
        except asyncio.CancelledError:
            logger.info("Attribution engine task stopping")
            break
        except Exception as exc:
            logger.error("Attribution cycle failed", exc_info=exc)
