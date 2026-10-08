"""
Modus — Alerts Router
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import Alert
from orchestrator.db.session import get_session

router = APIRouter()


class AlertResponse(BaseModel):
    id: str
    threshold_id: str
    severity: str
    metric: str
    threshold_value: Decimal
    actual_value: Decimal
    app_id: Optional[str]
    team_id: str
    fired_at: datetime
    acknowledged_at: Optional[datetime]
    acknowledged_by: Optional[str]


@router.get("/alerts", response_model=list[AlertResponse], tags=["alerts"])
async def list_alerts(
    team_id: Optional[str] = None,
    unacknowledged_only: bool = False,
    limit: int = 50,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[AlertResponse]:
    identity.assert_permission("alerts:read")
    q = select(Alert)
    if team_id:
        identity.assert_team_access(team_id)
        q = q.where(Alert.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(Alert.team_id.in_(identity.team_ids))
    if unacknowledged_only:
        q = q.where(Alert.acknowledged_at.is_(None))
    q = q.order_by(Alert.fired_at.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [AlertResponse(id=str(a.id), threshold_id=str(a.threshold_id),
                           severity=a.severity, metric=a.metric,
                           threshold_value=a.threshold_value, actual_value=a.actual_value,
                           app_id=str(a.app_id) if a.app_id else None,
                           team_id=str(a.team_id), fired_at=a.fired_at,
                           acknowledged_at=a.acknowledged_at,
                           acknowledged_by=a.acknowledged_by)
            for a in rows]


class AcknowledgeResponse(BaseModel):
    status: str


@router.post(
    "/alerts/{alert_id}/acknowledge",
    response_model=AcknowledgeResponse,
    tags=["alerts"],
)
async def acknowledge_alert(
    alert_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> AcknowledgeResponse:
    identity.assert_permission("alerts:write")
    alert = await db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found.")
    identity.assert_team_access(str(alert.team_id))
    alert.acknowledged_at = datetime.now(timezone.utc)
    alert.acknowledged_by = identity.actor_id
    return AcknowledgeResponse(status="acknowledged")
