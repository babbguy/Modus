"""
Modus Conductor — Data Ingest (Push/Ack Protocol)
=========================================================
Receives aggregated data from Orchestrators via push.

Protocol:
  1. Orchestrator sends POST /api/v1/conductor/push with:
     - Batch ID (for exactly-once delivery)
     - Aggregated usage data
     - Team/app metadata
     - Recent alerts
     - Policy decisions (for executive charts)
  2. Conductor validates, deduplicates, and stores
  3. Conductor returns acknowledgement with receipt ID

Design priorities (same as Orchestrator ingest):
  1. Fast — minimal processing on the hot path
  2. Idempotent — duplicate batch_ids are silently acknowledged
  3. Resilient — partial data doesn't reject the batch
  4. Secure — every push authenticated via conductor_secret

Federation-ready: a Federation layer can use the exact same push
protocol to deliver cross-region aggregates to a global Conductor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from conductor.core.auth import verify_orchestrator_push
from conductor.core.cache import CacheTier, cache
from conductor.core.config import settings
from conductor.db.models import (
    AlertCache,
    AppCache,
    ConductorAggregate,
    OrchestratorNode,
    PolicyDecisionCache,
    PushReceipt,
    TeamCache,
)
from conductor.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conductor")


# ── Request Models ─────────────────────────────────────────────────────────────


class AggregatePayload(BaseModel):
    app_id: str
    app_name: str = ""
    team_id: str
    team_slug: str = ""
    team_name: str = ""
    provider: str
    model: Optional[str] = None
    resource_type: str = "llm_call"
    environment: str = "production"
    granularity: str  # "hourly" or "daily"
    period_start: datetime
    period_end: datetime
    call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cost: Decimal = Decimal("0")
    output_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    avg_duration_ms: Optional[int] = None
    p95_duration_ms: Optional[int] = None


class TeamPayload(BaseModel):
    id: str
    slug: str
    name: str
    department: Optional[str] = None
    max_budget_usd: Optional[Decimal] = None
    budget_monthly_usd: Optional[Decimal] = None
    budget_quarterly_usd: Optional[Decimal] = None
    cost_center_code: Optional[str] = None
    cost_center_name: Optional[str] = None


class AppPayload(BaseModel):
    id: str
    app_id: str
    app_name: str
    team_id: str
    environment: str = "production"
    is_active: bool = True
    last_seen_at: Optional[datetime] = None
    agent_version: Optional[str] = None
    instrumented_providers: Optional[list[str]] = None


class AlertPayload(BaseModel):
    id: str
    severity: str
    metric: str
    threshold_value: Decimal
    actual_value: Decimal
    app_id: Optional[str] = None
    team_id: str
    fired_at: datetime
    acknowledged: bool = False


class PolicyDecisionPayload(BaseModel):
    id: str
    decision: str  # allow, deny, throttle
    estimated_cost: Decimal = Decimal("0")
    decided_at: datetime
    team_id: Optional[str] = None
    app_id: Optional[str] = None


class PushRequest(BaseModel):
    batch_id: str
    aggregates: list[AggregatePayload] = []
    teams: list[TeamPayload] = []
    apps: list[AppPayload] = []
    alerts: list[AlertPayload] = []
    policy_decisions: list[PolicyDecisionPayload] = []

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id(cls, v: str) -> str:
        if not v or len(v) < 8 or len(v) > 128:
            raise ValueError("batch_id must be 8-128 characters")
        return v


class PushResponse(BaseModel):
    receipt_id: str
    batch_id: str
    status: str  # "accepted" or "duplicate"
    aggregates_accepted: int
    teams_updated: int
    apps_updated: int
    alerts_accepted: int
    policy_decisions_accepted: int


# ── Endpoint ───────────────────────────────────────────────────────────────────


@router.post("/push", response_model=PushResponse, tags=["ingest"])
async def push_data(
    body: PushRequest,
    instance_id: str = Depends(verify_orchestrator_push),
    db: AsyncSession = Depends(get_session),
):
    """
    Receive aggregated data from an Orchestrator.

    Idempotent: duplicate batch_ids return the original receipt.
    """
    now = datetime.now(timezone.utc)

    # 1. Look up the Orchestrator
    orch_result = await db.execute(
        select(OrchestratorNode).where(
            OrchestratorNode.instance_id == instance_id
        )
    )
    orch_node = orch_result.scalar_one_or_none()
    if not orch_node:
        raise HTTPException(
            status_code=404,
            detail=f"Orchestrator {instance_id} not registered",
        )

    # 2. Check for duplicate batch
    dup_result = await db.execute(
        select(PushReceipt).where(
            PushReceipt.orchestrator_node_id == orch_node.id,
            PushReceipt.batch_id == body.batch_id,
        )
    )
    existing_receipt = dup_result.scalar_one_or_none()
    if existing_receipt:
        logger.debug("Duplicate push batch_id=%s from %s", body.batch_id, instance_id)
        return PushResponse(
            receipt_id=existing_receipt.id,
            batch_id=body.batch_id,
            status="duplicate",
            aggregates_accepted=0,
            teams_updated=0,
            apps_updated=0,
            alerts_accepted=0,
            policy_decisions_accepted=0,
        )

    # 3. Process aggregates — upsert into conductor_aggregates
    agg_count = 0
    total_batch_cost = Decimal("0")

    for agg in body.aggregates:
        import uuid as _uuid_mod
        agg_id = str(_uuid_mod.uuid4())
        # Use SQLite or PostgreSQL insert depending on backend
        if settings.is_sqlite:
            stmt = sqlite_insert(ConductorAggregate).values(
                id=agg_id,
                orchestrator_node_id=orch_node.id,
                app_id=agg.app_id,
                app_name=agg.app_name,
                team_id=agg.team_id,
                team_slug=agg.team_slug,
                team_name=agg.team_name,
                provider=agg.provider,
                model=agg.model,
                resource_type=agg.resource_type,
                environment=agg.environment,
                granularity=agg.granularity,
                period_start=agg.period_start,
                period_end=agg.period_end,
                call_count=agg.call_count,
                input_tokens=agg.input_tokens,
                output_tokens=agg.output_tokens,
                total_tokens=agg.total_tokens,
                input_cost=agg.input_cost,
                output_cost=agg.output_cost,
                total_cost=agg.total_cost,
                avg_duration_ms=agg.avg_duration_ms,
                p95_duration_ms=agg.p95_duration_ms,
                region_id=orch_node.region_id,
            ).on_conflict_do_update(
                index_elements=[
                    "orchestrator_node_id", "app_id", "team_id",
                    "provider", "model", "period_start", "granularity",
                ],
                set_={
                    "call_count": agg.call_count,
                    "input_tokens": agg.input_tokens,
                    "output_tokens": agg.output_tokens,
                    "total_tokens": agg.total_tokens,
                    "input_cost": agg.input_cost,
                    "output_cost": agg.output_cost,
                    "total_cost": agg.total_cost,
                    "avg_duration_ms": agg.avg_duration_ms,
                    "p95_duration_ms": agg.p95_duration_ms,
                    "app_name": agg.app_name,
                    "team_slug": agg.team_slug,
                    "team_name": agg.team_name,
                    "environment": agg.environment,
                },
            )
        else:
            stmt = pg_insert(ConductorAggregate).values(
                id=agg_id,
                orchestrator_node_id=orch_node.id,
                app_id=agg.app_id,
                app_name=agg.app_name,
                team_id=agg.team_id,
                team_slug=agg.team_slug,
                team_name=agg.team_name,
                provider=agg.provider,
                model=agg.model,
                resource_type=agg.resource_type,
                environment=agg.environment,
                granularity=agg.granularity,
                period_start=agg.period_start,
                period_end=agg.period_end,
                call_count=agg.call_count,
                input_tokens=agg.input_tokens,
                output_tokens=agg.output_tokens,
                total_tokens=agg.total_tokens,
                input_cost=agg.input_cost,
                output_cost=agg.output_cost,
                total_cost=agg.total_cost,
                avg_duration_ms=agg.avg_duration_ms,
                p95_duration_ms=agg.p95_duration_ms,
                region_id=orch_node.region_id,
            ).on_conflict_do_update(
                constraint="uq_conductor_agg",
                set_={
                    "call_count": agg.call_count,
                    "input_tokens": agg.input_tokens,
                    "output_tokens": agg.output_tokens,
                    "total_tokens": agg.total_tokens,
                    "input_cost": agg.input_cost,
                    "output_cost": agg.output_cost,
                    "total_cost": agg.total_cost,
                    "avg_duration_ms": agg.avg_duration_ms,
                    "p95_duration_ms": agg.p95_duration_ms,
                    "app_name": agg.app_name,
                    "team_slug": agg.team_slug,
                    "team_name": agg.team_name,
                    "environment": agg.environment,
                },
            )
        await db.execute(stmt)
        agg_count += 1
        total_batch_cost += agg.total_cost

    # 4. Upsert team metadata
    teams_updated = 0
    for team in body.teams:
        existing_team = await db.get(TeamCache, team.id)
        if existing_team:
            existing_team.slug = team.slug
            existing_team.name = team.name
            existing_team.department = team.department
            existing_team.max_budget_usd = team.max_budget_usd
            existing_team.budget_monthly_usd = team.budget_monthly_usd
            existing_team.budget_quarterly_usd = team.budget_quarterly_usd
            existing_team.cost_center_code = team.cost_center_code
            existing_team.cost_center_name = team.cost_center_name
            existing_team.source_orchestrator_id = orch_node.id
            existing_team.updated_at = now
        else:
            db.add(TeamCache(
                id=team.id,
                slug=team.slug,
                name=team.name,
                department=team.department,
                max_budget_usd=team.max_budget_usd,
                budget_monthly_usd=team.budget_monthly_usd,
                budget_quarterly_usd=team.budget_quarterly_usd,
                cost_center_code=team.cost_center_code,
                cost_center_name=team.cost_center_name,
                source_orchestrator_id=orch_node.id,
                updated_at=now,
            ))
        teams_updated += 1

    # 5. Upsert app metadata
    apps_updated = 0
    for app in body.apps:
        existing_app = await db.get(AppCache, app.id)
        if existing_app:
            existing_app.app_id = app.app_id
            existing_app.app_name = app.app_name
            existing_app.team_id = app.team_id
            existing_app.environment = app.environment
            existing_app.is_active = app.is_active
            existing_app.last_seen_at = app.last_seen_at
            existing_app.agent_version = app.agent_version
            existing_app.instrumented_providers = app.instrumented_providers
            existing_app.source_orchestrator_id = orch_node.id
            existing_app.updated_at = now
        else:
            db.add(AppCache(
                id=app.id,
                app_id=app.app_id,
                app_name=app.app_name,
                team_id=app.team_id,
                environment=app.environment,
                is_active=app.is_active,
                last_seen_at=app.last_seen_at,
                agent_version=app.agent_version,
                instrumented_providers=app.instrumented_providers,
                source_orchestrator_id=orch_node.id,
                updated_at=now,
            ))
        apps_updated += 1

    # 6. Insert alerts
    alerts_accepted = 0
    for alert in body.alerts:
        existing_alert = await db.get(AlertCache, alert.id)
        if not existing_alert:
            db.add(AlertCache(
                id=alert.id,
                severity=alert.severity,
                metric=alert.metric,
                threshold_value=alert.threshold_value,
                actual_value=alert.actual_value,
                app_id=alert.app_id,
                team_id=alert.team_id,
                fired_at=alert.fired_at,
                acknowledged=alert.acknowledged,
                source_orchestrator_id=orch_node.id,
            ))
            alerts_accepted += 1

    # 7. Insert policy decisions
    decisions_accepted = 0
    for pd in body.policy_decisions:
        existing_pd = await db.get(PolicyDecisionCache, pd.id)
        if not existing_pd:
            db.add(PolicyDecisionCache(
                id=pd.id,
                decision=pd.decision,
                estimated_cost=pd.estimated_cost,
                decided_at=pd.decided_at,
                team_id=pd.team_id,
                app_id=pd.app_id,
                source_orchestrator_id=orch_node.id,
            ))
            decisions_accepted += 1

    # 8. Create push receipt
    import uuid as _uuid
    receipt_id = str(_uuid.uuid4())
    receipt = PushReceipt(
        id=receipt_id,
        orchestrator_node_id=orch_node.id,
        batch_id=body.batch_id,
        aggregate_count=agg_count,
        total_cost_in_batch=total_batch_cost,
        received_at=now,
        acknowledged=True,
    )
    db.add(receipt)

    # 9. Update Orchestrator last_push_at
    orch_node.last_push_at = now
    orch_node.consecutive_missed_pushes = 0

    # 10. Invalidate cache — new data arrived
    await cache.invalidate_tier(CacheTier.HOT)
    await cache.invalidate_tier(CacheTier.WARM)

    logger.info(
        "Push accepted: batch=%s from=%s aggs=%d teams=%d apps=%d alerts=%d decisions=%d cost=$%.4f",
        body.batch_id, instance_id, agg_count, teams_updated, apps_updated,
        alerts_accepted, decisions_accepted, float(total_batch_cost),
    )

    return PushResponse(
        receipt_id=receipt.id,
        batch_id=body.batch_id,
        status="accepted",
        aggregates_accepted=agg_count,
        teams_updated=teams_updated,
        apps_updated=apps_updated,
        alerts_accepted=alerts_accepted,
        policy_decisions_accepted=decisions_accepted,
    )
