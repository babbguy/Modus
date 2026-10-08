"""
Modus — External Connections API
==========================================
Admin endpoints for viewing and testing all external connections
from a Modus deployment.

Endpoints:
    GET  /api/v1/admin/connections                     — list all connections + cached status
    POST /api/v1/admin/connections/{connection_id}/test — test a specific connection
    POST /api/v1/admin/connections/test-all             — test all connections in parallel
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.connection_checker import (
    gather_all_connections,
    sanitize_connection,
    test_all_connections,
    test_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/connections", tags=["connections"])


# ── Response models ──────────────────────────────────────────────────────────

class ConnectionInfo(BaseModel):
    id: str
    category: str
    name: str
    type: str
    endpoint: str
    status: str
    last_check: Optional[str] = None
    last_success: Optional[str] = None
    last_error: Optional[str] = None
    metadata: dict = {}

    class Config:
        from_attributes = True


class ConnectionsResponse(BaseModel):
    connections: list[ConnectionInfo]
    summary: dict
    checked_at: str

    class Config:
        from_attributes = True


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_summary(connections: list[dict]) -> dict:
    """Build a summary of connection statuses."""
    summary = {
        "total": len(connections),
        "connected": 0,
        "degraded": 0,
        "error": 0,
        "disabled": 0,
        "not_configured": 0,
    }
    for conn in connections:
        s = conn.get("status", "not_configured")
        if s == "connected":
            summary["connected"] += 1
        elif s == "degraded":
            summary["degraded"] += 1
        elif s in ("error", "unreachable", "expired"):
            summary["error"] += 1
        elif s == "disabled":
            summary["disabled"] += 1
        elif s == "not_configured":
            summary["not_configured"] += 1
        # "unknown" and others don't increment any specific counter
    return summary


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=ConnectionsResponse,
    summary="List all external connections",
)
async def list_connections(
    identity: Identity = Depends(get_identity),
) -> ConnectionsResponse:
    """
    Return all configured external connections with their cached status.
    Does not perform active connectivity tests — uses cached results
    from previous tests (60-second TTL).
    """
    raw_connections = await gather_all_connections()
    connections = [sanitize_connection(c) for c in raw_connections]
    return ConnectionsResponse(
        connections=[ConnectionInfo(**c) for c in connections],
        summary=_build_summary(connections),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/{connection_id}/test",
    response_model=ConnectionInfo,
    summary="Test a specific connection",
)
async def test_single_connection(
    connection_id: str,
    identity: Identity = Depends(get_identity),
) -> ConnectionInfo:
    """
    Actively test a specific connection by ID. Bypasses the cache
    and performs a live connectivity check with a 3-second timeout.
    """
    try:
        result = await test_connection(connection_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connection '{connection_id}' not found.",
        )
    except Exception as exc:
        logger.error("Connection test error for %s: %s", connection_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Test failed: {type(exc).__name__}",
        )
    return ConnectionInfo(**sanitize_connection(result))


@router.post(
    "/test-all",
    response_model=ConnectionsResponse,
    summary="Test all connections",
)
async def test_all(
    identity: Identity = Depends(get_identity),
) -> ConnectionsResponse:
    """
    Actively test all configured connections in parallel.
    Each connection is tested with a 3-second timeout.
    Results are cached for 60 seconds.
    """
    try:
        raw_results = await test_all_connections()
    except Exception as exc:
        logger.error("Test-all failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Test failed: {type(exc).__name__}",
        )
    connections = [sanitize_connection(c) for c in raw_results]
    return ConnectionsResponse(
        connections=[ConnectionInfo(**c) for c in connections],
        summary=_build_summary(connections),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
