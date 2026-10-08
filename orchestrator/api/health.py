"""
Modus — Health / Readiness Endpoints
=============================================
GET /health    — liveness: is the process alive?
GET /ready     — readiness: is the process ready to serve traffic?
GET /version   — version info

Kubernetes uses:
    livenessProbe  → GET /health  (restart if fails)
    readinessProbe → GET /ready   (remove from LB if fails)

/health never checks external dependencies — just returns 200.
If the process can respond, it's alive.

/ready checks that all dependencies (database) are reachable.
If DB is down, /ready returns 503 and k8s stops routing traffic to this pod
while still keeping it alive (no restart, just removed from load balancer).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from orchestrator.core.config import settings
from orchestrator.db.session import get_engine

logger = logging.getLogger(__name__)
router = APIRouter()

_start_time = time.time()


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    timestamp: datetime


class ReadinessResponse(BaseModel):
    status: str
    version: str
    checks: dict[str, str]
    timestamp: datetime


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    tags=["health"],
)
async def health() -> HealthResponse:
    """Always returns 200 if the process is running."""
    return HealthResponse(
        status="ok",
        version=settings.version,
        uptime_seconds=round(time.time() - _start_time, 1),
        timestamp=datetime.now(timezone.utc),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    tags=["health"],
)
async def ready() -> ReadinessResponse:
    """
    Returns 200 if all dependencies are healthy, 503 otherwise.
    Kubernetes readiness probe — removes pod from load balancer if unhealthy.
    """
    from fastapi.responses import JSONResponse

    checks: dict[str, str] = {}
    all_healthy = True

    # Database check
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("Readiness check: database unhealthy", exc_info=exc)
        checks["database"] = f"error: {type(exc).__name__}"
        all_healthy = False

    response_data = ReadinessResponse(
        status="ok" if all_healthy else "degraded",
        version=settings.version,
        checks=checks,
        timestamp=datetime.now(timezone.utc),
    )

    if not all_healthy:
        return JSONResponse(
            status_code=503,
            content=response_data.model_dump(mode="json"),
        )

    return response_data


class VersionResponse(BaseModel):
    version: str
    environment: str
    auth_mode: str


@router.get(
    "/version",
    response_model=VersionResponse,
    summary="Version info",
    tags=["health"],
)
async def version() -> VersionResponse:
    return VersionResponse(
        version=settings.version,
        environment=settings.environment,
        auth_mode=settings.auth_mode,
    )


# ── Gateway status (for SDK health checks + dashboard widget) ─────────────

class GatewayStatusResponse(BaseModel):
    evaluate_available: bool
    enforcement_enabled: bool
    enforcement_fail_open: bool
    enforcement_timeout_ms: int
    uptime_seconds: float
    database_ok: bool
    timestamp: datetime


@router.get(
    "/gateway/status",
    response_model=GatewayStatusResponse,
    summary="Evaluate gateway health",
    tags=["health"],
)
async def gateway_status() -> GatewayStatusResponse:
    """
    Reports whether the evaluate gateway is operational.
    SDKs and dashboards poll this to show gateway health.
    """
    db_ok = True
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return GatewayStatusResponse(
        evaluate_available=db_ok and settings.enforcement_enabled,
        enforcement_enabled=settings.enforcement_enabled,
        enforcement_fail_open=settings.enforcement_fail_open,
        enforcement_timeout_ms=settings.enforcement_timeout_ms,
        uptime_seconds=round(time.time() - _start_time, 1),
        database_ok=db_ok,
        timestamp=datetime.now(timezone.utc),
    )


# ── Diagnostic / Speed Test ───────────────────────────────────────────────

class DiagnosticResult(BaseModel):
    status: str  # "pass" or "fail"
    uptime_seconds: float
    version: str
    database: dict
    write_queue: dict
    background_tasks: dict
    memory: dict
    timestamp: datetime


@router.get(
    "/diagnostic",
    response_model=DiagnosticResult,
    summary="System diagnostic and speed test",
    tags=["health"],
)
async def run_diagnostic() -> DiagnosticResult:
    """
    Comprehensive system diagnostic — checks database latency, write queue depth,
    background task health, and memory usage. Used by the admin dashboard.
    """
    import asyncio
    import os

    overall_status = "pass"

    # Database speed test — measure read latency
    db_result = {"status": "ok", "read_latency_ms": 0, "query_latency_ms": 0}
    try:
        engine = get_engine()
        # Read test
        t0 = time.perf_counter()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_result["read_latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Query test (measure a typical indexed count query)
        t0 = time.perf_counter()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT COUNT(*) FROM usage_records LIMIT 1"))
        db_result["query_latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    except Exception as exc:
        logger.error("Diagnostic DB check failed: %s", exc, exc_info=True)
        db_result["status"] = f"error: {type(exc).__name__}"
        overall_status = "fail"

    # Write queue stats
    wq_result = {"status": "ok", "queue_depth": 0, "writer_active": False}
    try:
        from orchestrator.core.write_queue import _queue, _writer_task
        wq_result["queue_depth"] = _queue.qsize() if _queue else 0
        wq_result["writer_active"] = _writer_task is not None and not _writer_task.done()
        if not wq_result["writer_active"]:
            wq_result["status"] = "warning: writer task not running"
    except Exception as exc:
        wq_result["status"] = f"error: {exc}"

    # Background tasks
    bg_result: dict[str, Any] = {"status": "ok", "active": [], "failed": []}
    for task in asyncio.all_tasks():
        name = task.get_name()
        if name in ("aggregation", "maintenance", "pricing_sync", "anomaly_scan",
                     "forecast", "recommendations", "routing_calibrator", "drift_monitor",
                     "db-writer"):
            if task.done():
                bg_result["failed"].append(name)
            else:
                bg_result["active"].append(name)
    if bg_result["failed"]:
        bg_result["status"] = f"warning: {len(bg_result['failed'])} task(s) stopped"

    # Memory usage — use /proc/self/status on Linux, fallback gracefully elsewhere
    mem_result = {"status": "ok", "rss_mb": 0, "pid": os.getpid()}
    try:
        import sys
        if sys.platform == "linux":
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        mem_result["rss_mb"] = round(rss_kb / 1024, 1)
                        break
        else:
            mem_result["status"] = "unavailable (non-Linux platform)"
    except Exception:
        mem_result["status"] = "unavailable"

    return DiagnosticResult(
        status=overall_status,
        uptime_seconds=round(time.time() - _start_time, 1),
        version=settings.version,
        database=db_result,
        write_queue=wq_result,
        background_tasks=bg_result,
        memory=mem_result,
        timestamp=datetime.now(timezone.utc),
    )
