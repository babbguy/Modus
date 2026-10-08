"""
Tests for orchestrator.core.tasks — Background task loop structure.
Verifies that each task loop function exists, is async, and handles
CancelledError for clean shutdown.
"""
from __future__ import annotations

import asyncio

import pytest

from orchestrator.core import tasks


class TestTaskFunctions:
    """Verify all task loop functions are defined and async."""

    TASK_FUNCS = [
        "_run_aggregation_loop",
        "_run_maintenance_loop",
        "_run_pricing_sync_loop",
        "_run_anomaly_loop",
        "_run_forecast_loop",
        "_run_recommendations_loop",
        "_run_efficiency_audit_loop",
        "_run_governance_loop",
        "_run_trajectory_fingerprint_loop",
        "_run_trism_pattern_sync_loop",
        "_run_evolution_loop_task",
        "_run_neuromorphic_metrics_loop",
        "_run_neuro_assurance_loop",
        "_run_federation_sync_loop",
        "_run_pqc_assessment_loop",
        "_run_report_scheduler_loop",
        "_run_routing_calibrator_loop",
        "_run_drift_monitor_loop",
        "_run_attribution_loop",
    ]

    def test_all_task_functions_exist(self):
        for name in self.TASK_FUNCS:
            assert hasattr(tasks, name), f"Missing task function: {name}"

    def test_all_task_functions_are_coroutines(self):
        for name in self.TASK_FUNCS:
            fn = getattr(tasks, name)
            assert asyncio.iscoroutinefunction(fn), (
                f"{name} should be an async function"
            )


class TestAggregationLoopCancellation:
    """The aggregation loop should exit cleanly on CancelledError."""

    @pytest.mark.asyncio
    async def test_cancel_stops_loop(self):
        task = asyncio.create_task(tasks._run_aggregation_loop())
        await asyncio.sleep(0.01)
        task.cancel()
        # The loop catches CancelledError internally and exits cleanly
        await task  # Should complete without raising


class TestMaintenanceLoopCancellation:
    @pytest.mark.asyncio
    async def test_cancel_stops_loop(self):
        task = asyncio.create_task(tasks._run_maintenance_loop())
        await asyncio.sleep(0.01)
        task.cancel()
        await task  # Should complete without raising


class TestAttributionLoopCancellation:
    @pytest.mark.asyncio
    async def test_cancel_stops_loop(self):
        task = asyncio.create_task(tasks._run_attribution_loop())
        await asyncio.sleep(0.01)
        task.cancel()
        await task  # Should complete without raising
