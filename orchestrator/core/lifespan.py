"""
FastAPI lifespan handler for the Modus Orchestrator.

Manages startup (DB init, write queue, plugins, background
tasks) and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.core.config import settings
from orchestrator.core.logging_config import configure_logging
from orchestrator.core.tasks import (
    _run_aggregation_loop,
    _run_anomaly_loop,
    _run_attribution_loop,
    _run_drift_monitor_loop,
    _run_efficiency_audit_loop,
    _run_evolution_loop_task,
    _run_federation_sync_loop,
    _run_forecast_loop,
    _run_governance_loop,
    _run_maintenance_loop,
    _run_neuro_assurance_loop,
    _run_neuromorphic_metrics_loop,
    _run_pqc_assessment_loop,
    _run_pricing_sync_loop,
    _run_recommendations_loop,
    _run_report_scheduler_loop,
    _run_routing_calibrator_loop,
    _run_trajectory_fingerprint_loop,
    _run_trism_pattern_sync_loop,
)
from orchestrator.db.session import close_db, init_db
from orchestrator.metrics.prometheus import APP_INFO

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler.
    Replaces @app.on_event("startup") / ("shutdown") — deprecated in FastAPI 0.93+.
    """
    # ── Startup ────────────────────────────────────────────────────────────────
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    logger.info(
        "Modus Orchestrator starting",
        extra={
            "version": settings.version,
            "environment": settings.environment,
            "auth_mode": settings.auth_mode,
            "metrics_enabled": settings.metrics_enabled,
        },
    )

    await init_db()

    # Start the write queue (must be after init_db so _session_factory is ready)
    from orchestrator.core.write_queue import start_writer, stop_writer
    await start_writer()

    # Load plugins from plugins/ directory
    if settings.plugins_enabled:
        from orchestrator.core.plugin_loader import load_plugins
        loaded = load_plugins()
        if loaded:
            logger.info("Plugins loaded", extra={"plugins": list(loaded.keys())})

    APP_INFO.info({
        "version": settings.version,
        "environment": settings.environment,
        "auth_mode": settings.auth_mode,
    })

    # Start background tasks
    tasks = [
        asyncio.create_task(_run_aggregation_loop(), name="aggregation"),
        asyncio.create_task(_run_maintenance_loop(), name="maintenance"),
        asyncio.create_task(_run_pricing_sync_loop(), name="pricing_sync"),
    ]

    # Nomus regulatory sync (optional): idle until MODUS_NOMUS_URL is configured
    from orchestrator.core.nomus_client import nomus_sync_loop
    tasks.append(asyncio.create_task(nomus_sync_loop(), name="nomus_sync"))

    tasks.extend([
        asyncio.create_task(_run_anomaly_loop(), name="anomaly_scan"),
        asyncio.create_task(_run_forecast_loop(), name="forecast"),
        asyncio.create_task(_run_recommendations_loop(), name="recommendations"),
        asyncio.create_task(_run_efficiency_audit_loop(), name="efficiency_audit"),
        asyncio.create_task(_run_report_scheduler_loop(), name="report_scheduler"),
        asyncio.create_task(_run_attribution_loop(), name="attribution"),
        asyncio.create_task(_run_governance_loop(), name="governance_loop"),
        asyncio.create_task(_run_trajectory_fingerprint_loop(), name="trajectory_fingerprints"),
        asyncio.create_task(_run_trism_pattern_sync_loop(), name="trism_pattern_sync"),
        asyncio.create_task(_run_evolution_loop_task(), name="constitutional_evolution"),
        asyncio.create_task(_run_neuromorphic_metrics_loop(), name="neuromorphic_metrics"),
    ])
    if settings.routing_enabled:
        tasks.append(asyncio.create_task(_run_routing_calibrator_loop(), name="routing_calibrator"))
        tasks.append(asyncio.create_task(_run_drift_monitor_loop(), name="drift_monitor"))

    # Optional subsystems, enabled by settings
    if settings.neuro_assurance_enabled:
        tasks.append(asyncio.create_task(_run_neuro_assurance_loop(), name="neuro_assurance_benchmark"))
    if settings.federation_enabled:
        tasks.append(asyncio.create_task(_run_federation_sync_loop(), name="federation_sync"))
    if settings.pqc_assessment_enabled:
        tasks.append(asyncio.create_task(_run_pqc_assessment_loop(), name="pqc_assessment"))

    # Conductor push — sends aggregated data to the org-level Conductor
    if settings.conductor_url:
        from orchestrator.core.conductor_push import run_conductor_push_loop
        tasks.append(asyncio.create_task(run_conductor_push_loop(), name="conductor_push"))

    logger.info("Background tasks started", extra={"tasks": [t.get_name() for t in tasks]})

    yield  # Application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("Modus Orchestrator shutting down")

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    await stop_writer()
    if settings.gateway_enabled:
        from orchestrator.api.gateway import close_gateway_client
        await close_gateway_client()
    await close_db()
    logger.info("Shutdown complete")
