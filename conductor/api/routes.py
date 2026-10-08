"""
Modus Conductor — Router Registration
=============================================
Registers all API routers on the FastAPI application.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from conductor.core.config import settings

logger = logging.getLogger(__name__)


def register_routers(app: FastAPI) -> None:
    """Register all Conductor API routers."""

    # ── Conductor API (Orchestrator-facing) ─────────────────────────────────
    from conductor.api.registry import router as registry_router
    from conductor.api.ingest import router as ingest_router

    app.include_router(registry_router, prefix="/api/v1", tags=["conductor"])
    app.include_router(ingest_router, prefix="/api/v1", tags=["conductor"])

    # ── Dashboard API (Dashboard-facing) ────────────────────────────────────
    from conductor.api.dashboard_data import router as dashboard_router

    app.include_router(dashboard_router, prefix="/api/v1", tags=["dashboard"])

    # ── Health ──────────────────────────────────────────────────────────────
    from conductor.api.health import router as health_router

    app.include_router(health_router, tags=["health"])

    # ── Dashboard static files ──────────────────────────────────────────────
    # The Conductor serves the dashboard since they're org-level co-residents.
    if not settings.disable_dashboard if hasattr(settings, "disable_dashboard") else True:
        _dashboard_dir = Path(__file__).resolve().parent.parent.parent / "dashboard"
        _dashboard_file = _dashboard_dir / "index.html"

        if _dashboard_file.exists():
            _dashboard_html = _dashboard_file.read_text(encoding="utf-8")

            @app.get("/", include_in_schema=False)
            async def serve_dashboard() -> HTMLResponse:
                return HTMLResponse(_dashboard_html)

            app.mount(
                "/dashboard",
                StaticFiles(directory=str(_dashboard_dir)),
                name="dashboard-static",
            )
            logger.info("Dashboard served from %s", _dashboard_dir)
        else:
            logger.warning("Dashboard not found at %s", _dashboard_dir)

    logger.info("Conductor routers registered")
