"""
Modus Conductor — Orchestrator Registry API
===================================================
Endpoints for Orchestrators to register themselves with the Conductor.

Registration flow:
  1. Orchestrator starts up
  2. Orchestrator POSTs to /api/v1/conductor/register with its metadata
  3. Conductor creates/updates the OrchestratorNode record
  4. Conductor returns a registration acknowledgement
  5. Orchestrator begins pushing data

Federation-ready: the same registration protocol can be used by a
future Federation layer to register regional Conductors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.core.auth import verify_orchestrator_push
from conductor.db.models import OrchestratorNode
from conductor.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conductor")


# ── Request / Response Models ──────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    name: str                             # Human-readable (e.g., "payments-api-prod")
    instance_id: str                      # Stable unique ID for this Orchestrator
    endpoint_url: Optional[str] = None    # URL where this Orchestrator can be reached
    version: Optional[str] = None         # Orchestrator version
    environment: Optional[str] = None     # production, staging, development
    app_count: int = 0                    # Number of apps managed
    agent_count: int = 0                  # Number of agents reporting
    region_id: str = ""                   # Region for federation


class RegisterResponse(BaseModel):
    orchestrator_node_id: str
    name: str
    instance_id: str
    status: str                           # "registered" or "updated"
    registered_at: datetime


class OrchestratorStatus(BaseModel):
    id: str
    name: str
    instance_id: str
    endpoint_url: Optional[str]
    version: Optional[str]
    environment: Optional[str]
    app_count: int
    agent_count: int
    region_id: str
    is_active: bool
    last_heartbeat_at: Optional[datetime]
    last_push_at: Optional[datetime]
    consecutive_missed_pushes: int
    registered_at: datetime


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("/register", response_model=RegisterResponse, tags=["registry"])
async def register_orchestrator(
    body: RegisterRequest,
    instance_id: str = Depends(verify_orchestrator_push),
    db: AsyncSession = Depends(get_session),
):
    """
    Register or update an Orchestrator with this Conductor.

    Idempotent: safe to call on every Orchestrator startup.
    """
    # Verify the instance_id in the header matches the body
    if body.instance_id != instance_id:
        raise HTTPException(
            status_code=400,
            detail="X-Orchestrator-Instance-ID header must match body.instance_id",
        )

    now = datetime.now(timezone.utc)

    # Check if already registered
    result = await db.execute(
        select(OrchestratorNode).where(
            OrchestratorNode.instance_id == body.instance_id
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Update existing registration
        existing.name = body.name
        existing.endpoint_url = body.endpoint_url
        existing.version = body.version
        existing.environment = body.environment
        existing.app_count = body.app_count
        existing.agent_count = body.agent_count
        existing.region_id = body.region_id
        existing.is_active = True
        existing.last_heartbeat_at = now
        existing.consecutive_missed_pushes = 0

        logger.info(
            "Orchestrator updated: %s (%s), %d apps, %d agents",
            body.name, body.instance_id, body.app_count, body.agent_count,
        )

        return RegisterResponse(
            orchestrator_node_id=existing.id,
            name=existing.name,
            instance_id=existing.instance_id,
            status="updated",
            registered_at=existing.registered_at,
        )

    # New registration
    node = OrchestratorNode(
        name=body.name,
        instance_id=body.instance_id,
        endpoint_url=body.endpoint_url,
        version=body.version,
        environment=body.environment,
        app_count=body.app_count,
        agent_count=body.agent_count,
        region_id=body.region_id,
        is_active=True,
        last_heartbeat_at=now,
        registered_at=now,
    )
    db.add(node)
    await db.flush()

    logger.info(
        "Orchestrator registered: %s (%s), %d apps, %d agents",
        body.name, body.instance_id, body.app_count, body.agent_count,
    )

    return RegisterResponse(
        orchestrator_node_id=node.id,
        name=node.name,
        instance_id=node.instance_id,
        status="registered",
        registered_at=node.registered_at,
    )


@router.get("/orchestrators", response_model=list[OrchestratorStatus], tags=["registry"])
async def list_orchestrators(
    db: AsyncSession = Depends(get_session),
):
    """List all registered Orchestrators and their health status."""
    result = await db.execute(
        select(OrchestratorNode).order_by(OrchestratorNode.name)
    )
    nodes = result.scalars().all()

    return [
        OrchestratorStatus(
            id=n.id,
            name=n.name,
            instance_id=n.instance_id,
            endpoint_url=n.endpoint_url,
            version=n.version,
            environment=n.environment,
            app_count=n.app_count,
            agent_count=n.agent_count,
            region_id=n.region_id,
            is_active=n.is_active,
            last_heartbeat_at=n.last_heartbeat_at,
            last_push_at=n.last_push_at,
            consecutive_missed_pushes=n.consecutive_missed_pushes,
            registered_at=n.registered_at,
        )
        for n in nodes
    ]


@router.post("/heartbeat", tags=["registry"])
async def orchestrator_heartbeat(
    instance_id: str = Depends(verify_orchestrator_push),
    db: AsyncSession = Depends(get_session),
):
    """
    Heartbeat from an Orchestrator. Updates last_heartbeat_at and resets
    the missed-push counter.
    """
    result = await db.execute(
        select(OrchestratorNode).where(
            OrchestratorNode.instance_id == instance_id
        )
    )
    node = result.scalar_one_or_none()

    if not node:
        raise HTTPException(
            status_code=404,
            detail=f"Orchestrator {instance_id} not registered. Call /conductor/register first.",
        )

    node.last_heartbeat_at = datetime.now(timezone.utc)
    node.consecutive_missed_pushes = 0
    node.is_active = True

    return {"status": "ok", "instance_id": instance_id}
