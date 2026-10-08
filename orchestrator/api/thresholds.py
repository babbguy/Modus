"""
Modus — Thresholds Router
"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import AuditLog, Threshold
from orchestrator.db.session import get_session

router = APIRouter()


class ThresholdCreate(BaseModel):
    name: str = Field(..., max_length=256)
    team_id: str
    app_id: Optional[str] = None
    user_id: Optional[str] = None
    cost_center_id: Optional[str] = None
    scope: str = Field("app", pattern="^(app|team|provider|user|cost_center)$")
    provider: Optional[str] = None
    metric: str = Field(..., pattern="^(total_cost|input_tokens|output_tokens|call_count)$")
    period: str = Field(..., pattern="^(hourly|daily|weekly|monthly)$")
    warning_value: Optional[Decimal] = None
    critical_value: Decimal
    notify: Optional[dict] = None
    degradation_model: Optional[str] = None
    degradation_enabled: bool = False


class ThresholdUpdate(BaseModel):
    name: Optional[str] = None
    warning_value: Optional[Decimal] = None
    critical_value: Optional[Decimal] = None
    is_active: Optional[bool] = None
    notify: Optional[dict] = None
    degradation_model: Optional[str] = None
    degradation_enabled: Optional[bool] = None


class ThresholdResponse(BaseModel):
    id: str
    name: str
    team_id: str
    app_id: Optional[str] = None
    user_id: Optional[str] = None
    cost_center_id: Optional[str] = None
    scope: str
    provider: Optional[str] = None
    metric: str
    period: str
    warning_value: Optional[Decimal] = None
    critical_value: Decimal
    is_active: bool
    notify: Optional[dict] = None
    degradation_model: Optional[str] = None
    degradation_enabled: bool = False
    created_at: datetime


def _threshold_to_response(t: Threshold) -> ThresholdResponse:
    return ThresholdResponse(
        id=str(t.id), name=t.name, team_id=str(t.team_id),
        app_id=str(t.app_id) if t.app_id else None,
        user_id=str(t.user_id) if t.user_id else None,
        cost_center_id=str(t.cost_center_id) if t.cost_center_id else None,
        scope=t.scope, provider=t.provider, metric=t.metric, period=t.period,
        warning_value=t.warning_value, critical_value=t.critical_value,
        is_active=t.is_active, notify=t.notify,
        degradation_model=t.degradation_model,
        degradation_enabled=t.degradation_enabled,
        created_at=t.created_at,
    )


@router.get("/thresholds", response_model=list[ThresholdResponse], tags=["thresholds"])
async def list_thresholds(
    team_id: Optional[str] = None,
    user_id: Optional[str] = None,
    cost_center_id: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[ThresholdResponse]:
    identity.assert_permission("thresholds:read")
    q = select(Threshold).where(Threshold.is_active == True)
    if team_id:
        identity.assert_team_access(team_id)
        q = q.where(Threshold.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(Threshold.team_id.in_(identity.team_ids))
    if user_id:
        q = q.where(Threshold.user_id == user_id)
    if cost_center_id:
        q = q.where(Threshold.cost_center_id == cost_center_id)
    if scope:
        q = q.where(Threshold.scope == scope)
    rows = (await db.execute(
        q.order_by(Threshold.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return [_threshold_to_response(t) for t in rows]


@router.post("/thresholds", response_model=ThresholdResponse, status_code=201, tags=["thresholds"])
async def create_threshold(
    body: ThresholdCreate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> ThresholdResponse:
    identity.assert_permission("thresholds:write")
    identity.assert_team_access(body.team_id)

    t = Threshold(**body.model_dump())
    db.add(t)
    await db.flush()
    db.add(AuditLog(actor_id=identity.actor_id,
                    actor_ip=request.client.host if request.client else None,
                    team_id=body.team_id, resource_type="threshold",
                    resource_id=str(t.id), action="created", after=body.model_dump(mode="json")))
    return _threshold_to_response(t)


@router.delete("/thresholds/{threshold_id}", status_code=204, response_model=None, tags=["thresholds"])
async def delete_threshold(
    threshold_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    identity.assert_permission("thresholds:delete")
    t = await db.get(Threshold, threshold_id)
    if not t:
        raise HTTPException(404, "Threshold not found.")
    identity.assert_team_access(str(t.team_id))
    t.is_active = False
    db.add(AuditLog(actor_id=identity.actor_id,
                    actor_ip=request.client.host if request.client else None,
                    team_id=str(t.team_id), resource_type="threshold",
                    resource_id=threshold_id, action="deleted"))


@router.patch("/thresholds/{threshold_id}", response_model=ThresholdResponse, tags=["thresholds"])
async def update_threshold(
    threshold_id: str,
    body: ThresholdUpdate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> ThresholdResponse:
    identity.assert_permission("thresholds:write")
    t = await db.get(Threshold, threshold_id)
    if not t:
        raise HTTPException(404, "Threshold not found.")
    identity.assert_team_access(str(t.team_id))

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(t, field, value)
    await db.flush()

    db.add(AuditLog(actor_id=identity.actor_id,
                    actor_ip=request.client.host if request.client else None,
                    team_id=str(t.team_id), resource_type="threshold",
                    resource_id=threshold_id, action="updated",
                    after=body.model_dump(mode="json", exclude_unset=True)))
    return _threshold_to_response(t)
