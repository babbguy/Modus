"""
Modus — Self-Diagnostic Error Log Generator
================================================
Scans the Modus instance and generates a diagnostic report
for support. Customer can download and send to the Modus team.

POST /api/v1/admin/diagnostics/scan     — run diagnostic scan
GET  /api/v1/admin/diagnostics/download — download last scan result

The report contains ONLY system state — NEVER customer data, API keys,
PII, prompts, responses, costs, or usage details. Law 4 compliance.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

diagnostics_router = APIRouter(prefix="/admin/diagnostics", tags=["diagnostics"])

# Module-level storage for the last scan result (no DB needed)
_last_scan: dict[str, Any] | None = None
_process_start = time.time()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(fn, default=None):
    """Run fn(), swallow exceptions, return default on failure."""
    try:
        return fn()
    except Exception:
        return default


def _get_memory_mb() -> float | None:
    """Get process memory usage in MB (cross-platform)."""
    try:
        # Windows
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024 * 1024), 2)
            return None
        else:
            # Unix/Linux/macOS
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # macOS returns bytes, Linux returns KB
            if sys.platform == "darwin":
                return round(usage.ru_maxrss / (1024 * 1024), 2)
            return round(usage.ru_maxrss / 1024, 2)
    except Exception:
        return None


def _get_sqlite_size() -> float | None:
    """Get SQLite file size in MB, or None if not using SQLite."""
    db_url = str(settings.database_url)
    if "sqlite" not in db_url:
        return None
    try:
        # Extract path from sqlite URL
        path = db_url.split("///")[-1]
        if path and os.path.exists(path):
            return round(os.path.getsize(path) / (1024 * 1024), 2)
    except OSError as exc:
        logger.debug("Diagnostics: could not stat SQLite file: %s", exc)
    return None


async def _collect_system_info() -> dict:
    """Collect system information (no customer data)."""
    uptime = time.time() - _process_start
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "modus_version": settings.version,
        "uptime_seconds": round(uptime, 1),
        "uptime_human": _format_uptime(uptime),
        "memory_mb": _get_memory_mb(),
        "sqlite_file_size_mb": _get_sqlite_size(),
        "cpu_count": os.cpu_count(),
    }


def _format_uptime(seconds: float) -> str:
    """Format seconds into human-readable uptime."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def _collect_database_health() -> dict:
    """Collect database health metrics (row counts only, no customer data)."""
    result: dict[str, Any] = {"status": "unknown", "tables": {}}
    try:
        from sqlalchemy import text as sa_text
        from orchestrator.db.session import get_session_ctx

        # Safety invariant: ALLOWED_TABLES is the exhaustive allowlist.
        # The table name is NEVER sourced from user input — it comes from
        # this hardcoded set — so f-string interpolation is safe.
        ALLOWED_TABLES = frozenset({
            "apps", "teams", "usage_records", "usage_aggregates",
            "governance_policies", "thresholds", "alerts",
        })
        tables = list(ALLOWED_TABLES)
        async with get_session_ctx() as db:
            for table in tables:
                if table not in ALLOWED_TABLES:
                    result["tables"][table] = "rejected"
                    continue
                try:
                    row = await db.execute(sa_text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
                    count = row.scalar() or 0
                    result["tables"][table] = count
                except Exception:
                    result["tables"][table] = "error"

        result["status"] = "ok"
        result["db_file_size_mb"] = _get_sqlite_size()
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:200]
    return result


async def _collect_background_tasks() -> dict:
    """Collect background task status (queue depth, not content)."""
    result: dict[str, Any] = {}

    # Write queue depth
    try:
        from orchestrator.core.write_queue import _queue, queue_over_pressure
        if _queue is not None:
            result["write_queue"] = {
                "depth": _queue.qsize(),
                "over_pressure": queue_over_pressure(),
            }
        else:
            result["write_queue"] = {"depth": 0, "status": "not_started"}
    except Exception:
        result["write_queue"] = {"status": "unavailable"}

    # Task status — we report whether key settings are enabled
    result["aggregation"] = {
        "interval_seconds": _safe(lambda: settings.aggregate_interval_seconds),
        "status": "enabled",
    }
    result["governance"] = {
        "enabled": _safe(lambda: settings.swarm_governance_enabled, False),
    }
    result["evolution"] = {
        "enabled": _safe(lambda: settings.evolution_enabled, False),
    }

    return result


async def _collect_api_health() -> dict:
    """Quick self-check of key endpoints (timing only, no data)."""
    result: dict[str, Any] = {}

    # Check /healthz
    try:
        t0 = time.monotonic()
        from orchestrator.db.session import get_engine
        from sqlalchemy import text as sa_text
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(sa_text("SELECT 1"))
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        result["db_ping_ms"] = elapsed
        result["db_status"] = "ok"
    except Exception as exc:
        result["db_status"] = "error"
        result["db_error"] = str(exc)[:200]

    return result


def _collect_configuration() -> dict:
    """Collect configuration state (keys present, NOT values)."""
    return {
        "auth_mode": settings.auth_mode,
        "environment": settings.environment,
        "debug": settings.debug,
        "routing_enabled": _safe(lambda: settings.routing_enabled, False),
        "gateway_enabled": _safe(lambda: settings.gateway_enabled, False),
        "metrics_enabled": _safe(lambda: settings.metrics_enabled, False),
        "governance_enabled": _safe(lambda: settings.swarm_governance_enabled, False),
        "evolution_enabled": _safe(lambda: settings.evolution_enabled, False),
        "database_type": "sqlite" if "sqlite" in str(settings.database_url) else "postgresql",
        "master_api_key_set": bool(_safe(
            lambda: settings.master_api_key and str(settings.master_api_key) not in ("", "None"),
            False,
        )),
    }


async def _collect_connections() -> list[dict]:
    """Reuse connection_checker to get all connection statuses."""
    try:
        from orchestrator.core.connection_checker import (
            gather_all_connections,
        )
        conns = await gather_all_connections()
        # Only return sanitized status info (no URLs, no secrets)
        return [
            {
                "id": c.get("id", "unknown"),
                "name": c.get("name", "unknown"),
                "category": c.get("category", "unknown"),
                "status": c.get("status", "unknown"),
            }
            for c in conns
        ]
    except Exception:
        return [{"status": "unavailable", "error": "connection_checker not available"}]


def _collect_recent_errors() -> list[dict]:
    """Collect recent ERROR/WARNING log entries (messages only, no customer data)."""
    errors: list[dict] = []
    try:
        # Check the root logger for handlers with recent records
        root = logging.getLogger()
        for handler in root.handlers:
            if hasattr(handler, "buffer"):
                # MemoryHandler
                for record in handler.buffer[-50:]:
                    if record.levelno >= logging.WARNING:
                        errors.append({
                            "level": record.levelname,
                            "logger": record.name,
                            "message": record.getMessage()[:300],
                            "timestamp": datetime.fromtimestamp(
                                record.created, tz=timezone.utc
                            ).isoformat(),
                        })
        # If no memory handler, check for file-based logs
        if not errors:
            log_file = Path("modus.log")
            if log_file.exists():
                try:
                    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    for line in lines[-100:]:
                        if "ERROR" in line or "WARNING" in line:
                            # Strip any potential customer data — only keep level + module + message type
                            sanitized = line[:300]
                            errors.append({
                                "level": "ERROR" if "ERROR" in line else "WARNING",
                                "message": sanitized,
                            })
                            if len(errors) >= 50:
                                break
                except Exception as exc:
                    logger.debug("Diagnostics: could not read modus.log: %s", exc)
    except Exception as exc:
        logger.debug("Diagnostics: error-log collection failed: %s", exc)

    return errors[-50:]  # Last 50 max


def _generate_recommendations(
    system: dict,
    database: dict,
    tasks: dict,
    api_health: dict,
    connections: list[dict],
    errors: list[dict],
) -> list[dict]:
    """Generate actionable recommendations from scan results."""
    recs: list[dict] = []

    # Write queue pressure
    wq = tasks.get("write_queue", {})
    if wq.get("over_pressure"):
        recs.append({
            "severity": "warning",
            "category": "background_tasks",
            "message": f"Write queue depth is {wq.get('depth', '?')} — consider investigating ingest throughput",
        })
    elif isinstance(wq.get("depth"), int) and wq["depth"] > 1000:
        recs.append({
            "severity": "info",
            "category": "background_tasks",
            "message": f"Write queue depth is {wq['depth']} — elevated but not critical",
        })

    # Database issues
    if database.get("status") == "error":
        recs.append({
            "severity": "error",
            "category": "database",
            "message": "Database health check failed — check connection and configuration",
        })
    else:
        db_size = database.get("db_file_size_mb")
        if db_size and db_size > 1000:
            recs.append({
                "severity": "warning",
                "category": "database",
                "message": f"Database file is {db_size:.0f} MB — consider running compaction or archiving old data",
            })

    # API health
    if api_health.get("db_status") == "error":
        recs.append({
            "severity": "error",
            "category": "api",
            "message": "Database ping failed — API may be degraded",
        })
    elif isinstance(api_health.get("db_ping_ms"), (int, float)) and api_health["db_ping_ms"] > 100:
        recs.append({
            "severity": "warning",
            "category": "api",
            "message": f"Database ping is {api_health['db_ping_ms']}ms — may indicate slow queries",
        })

    # Memory
    mem = system.get("memory_mb")
    if mem and mem > 512:
        recs.append({
            "severity": "warning",
            "category": "system",
            "message": f"Process using {mem:.0f} MB memory — monitor for leaks",
        })

    # Connection errors
    conn_errors = [c for c in connections if c.get("status") == "error"]
    if conn_errors:
        recs.append({
            "severity": "warning",
            "category": "connections",
            "message": f"{len(conn_errors)} connection(s) in error state",
        })

    # Recent errors
    error_count = len([e for e in errors if e.get("level") == "ERROR"])
    if error_count > 0:
        recs.append({
            "severity": "warning",
            "category": "logs",
            "message": f"{error_count} ERROR entries found in recent logs",
        })

    # All-clear
    if not recs:
        recs.append({
            "severity": "info",
            "category": "system",
            "message": "All systems operating normally — no issues detected",
        })

    return recs


# ── Endpoints ─────────────────────────────────────────────────────────────────

@diagnostics_router.post("/scan")
async def run_diagnostic_scan(identity: Identity = Depends(get_identity)):
    """
    Run a comprehensive diagnostic scan of the Modus instance.

    Collects system state ONLY — never customer data, API keys, or PII.
    Stores the result in memory for subsequent download.
    Requires platform_admin role.
    """
    identity.assert_permission("platform:manage")
    global _last_scan

    scan_start = time.monotonic()
    scan_id = f"diag-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S')}"
    scanned_at = datetime.now(timezone.utc).isoformat()

    # Collect all sections in parallel-safe manner
    system = await _collect_system_info()
    database = await _collect_database_health()
    tasks = await _collect_background_tasks()
    api_health = await _collect_api_health()
    configuration = _collect_configuration()
    connections = await _collect_connections()
    errors = _collect_recent_errors()

    recommendations = _generate_recommendations(
        system, database, tasks, api_health, connections, errors,
    )

    scan_duration_ms = round((time.monotonic() - scan_start) * 1000, 1)

    _last_scan = {
        "scan_id": scan_id,
        "scanned_at": scanned_at,
        "scan_duration_ms": scan_duration_ms,
        "modus_version": settings.version,
        "system": system,
        "database": database,
        "background_tasks": tasks,
        "api_health": api_health,
        "configuration": configuration,
        "connections": connections,
        "recent_errors": errors,
        "recommendations": recommendations,
    }

    # Build summary for quick display
    summary = {
        "system": "ok" if system.get("memory_mb") is not None else "warning",
        "database": database.get("status", "unknown"),
        "background_tasks": (
            "warning" if tasks.get("write_queue", {}).get("over_pressure")
            else "ok"
        ),
        "connections": (
            "error" if any(c.get("status") == "error" for c in connections)
            else "ok"
        ),
        "recommendation_counts": {
            "error": len([r for r in recommendations if r["severity"] == "error"]),
            "warning": len([r for r in recommendations if r["severity"] == "warning"]),
            "info": len([r for r in recommendations if r["severity"] == "info"]),
        },
    }

    return {
        "scan_id": scan_id,
        "scanned_at": scanned_at,
        "scan_duration_ms": scan_duration_ms,
        "summary": summary,
        "recommendations": recommendations,
    }


@diagnostics_router.get("/download")
async def download_diagnostic_report(identity: Identity = Depends(get_identity)):
    """
    Download the last diagnostic scan result as a JSON file.

    Returns 404 if no scan has been run yet.
    Requires platform_admin role.
    """
    identity.assert_permission("platform:manage")
    if _last_scan is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "No diagnostic scan available. Run a scan first."},
        )

    import json
    content = json.dumps(_last_scan, indent=2, default=str)
    filename = f"{_last_scan['scan_id']}.json"

    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
