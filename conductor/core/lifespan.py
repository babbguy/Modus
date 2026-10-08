"""
Modus Conductor — FastAPI Lifespan Handler
==================================================
Manages startup (DB init, background tasks) and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from conductor.core.config import settings
from conductor.core.logging_config import configure_logging
from conductor.core.tasks import _run_maintenance_loop, _run_reconciliation_loop
from conductor.db.session import close_db, init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────────────
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    logger.info(
        "Modus Conductor v%s starting (env=%s, region=%s)",
        settings.version,
        settings.environment,
        settings.region_id or "default",
    )

    await init_db()

    # Start background tasks
    tasks = [
        asyncio.create_task(_run_reconciliation_loop(), name="reconciliation"),
        asyncio.create_task(_run_maintenance_loop(), name="maintenance"),
    ]

    logger.info(
        "Conductor ready — background tasks: %s",
        [t.get_name() for t in tasks],
    )

    yield  # Application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("Modus Conductor shutting down")

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    await close_db()

    logger.info("Conductor shutdown complete")
