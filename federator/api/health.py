"""
Modus Federator — Health Endpoints
=========================================
Liveness and readiness probes for monitoring and orchestration.
"""
from __future__ import annotations

from fastapi import APIRouter

from federator.config import settings

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health() -> dict:
    """Liveness probe — returns 200 if the process is alive."""
    return {"status": "ok", "service": "modus-federator", "version": settings.version}


@health_router.get("/ready")
async def ready() -> dict:
    """Readiness probe — checks DB connectivity."""
    try:
        from federator.db.session import _engine
        async with _engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        return {"status": "not_ready", "database": str(e)}
