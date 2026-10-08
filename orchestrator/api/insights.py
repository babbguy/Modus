"""
Modus — Insights & Admin API
======================================
Endpoints powering the DevOps view, Executive view, and Admin settings panel.

GET  /api/v1/insights/anomalies            — recent anomaly events
GET  /api/v1/insights/recommendations      — model optimization recommendations
GET  /api/v1/insights/enforcement-summary  — policy decision counts + savings
GET  /api/v1/insights/forecast             — spend forecast per team
GET  /api/v1/reports/roi                   — ROI from enforcement (blocked calls)

GET  /api/v1/admin/settings                — all system settings (admin only)
PUT  /api/v1/admin/settings/{key}          — update a single setting (admin only)
POST /api/v1/admin/tasks/{task}/trigger    — trigger a task run immediately
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity, team_scope_clause
from orchestrator.core.insights_engine import (
    run_anomaly_scan, run_forecast_update,
    run_recommendation_refresh, _DEFAULTS,
)
from orchestrator.db.models import (
    AnomalyEvent, App, OptimizationRecommendation,
    SpendForecast, SystemSetting,
)
from orchestrator.db.session import get_session, sqlite_dt

logger = logging.getLogger(__name__)

insights_router = APIRouter(prefix="/insights", tags=["insights"])
reports_router  = APIRouter(prefix="/reports",  tags=["reports"])
admin_router    = APIRouter(prefix="/admin",     tags=["admin"])


# ── Response schemas ───────────────────────────────────────────────────────────

class AnomalyResponse(BaseModel):
    id: str
    app_id: Optional[str]
    app_name: Optional[str]
    team_id: str
    metric: str
    severity: str
    z_score: float
    baseline_value: float
    actual_value: float
    ai_explanation: Optional[str]
    detected_at: datetime
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]

    class Config:
        from_attributes = True


class RecommendationResponse(BaseModel):
    id: str
    app_id: str
    app_name: Optional[str]
    team_id: str
    provider: str
    current_model: str
    suggested_model: str
    call_volume_basis: int
    estimated_monthly_savings: float
    confidence: str
    recommendation_text: Optional[str]
    generated_at: datetime

    class Config:
        from_attributes = True


class EnforcementSummary(BaseModel):
    allowed: int
    blocked: int
    throttle: int
    redirect: int
    total_decisions: int
    total_savings: float
    period: str


class OpsKpis(BaseModel):
    """Live operational KPIs for the DevOps view (UTC day so far)."""
    window_start: datetime          # start of the UTC day these figures cover
    cost_this_hour: float           # spend in the current hourly bucket
    blocked_today: int              # policy decisions with decision == 'deny'
    throttled_today: int
    calls_today: int
    tokens_per_call: Optional[float]
    avg_latency_ms: Optional[float]  # call-weighted mean; percentiles are not collected
    latency_samples: int            # calls that reported a duration


class ForecastResponse(BaseModel):
    team_id: Optional[str]
    period_label: str
    mtd_actual: float
    forecast_eom: float
    forecast_eoq: Optional[float]
    forecast_eoy: Optional[float]
    trend_pct: Optional[float]
    r_squared: Optional[float]
    confidence_label: str
    computed_at: datetime


class RoiResponse(BaseModel):
    period: str
    blocked_calls: int
    estimated_savings: float
    top_policy: Optional[str]
    top_policy_blocks: Optional[int]
    platform_cost_usd: float  # Modus's own AI API costs (MTD); 0 until Modus meters its own usage
    net_savings: float        # estimated_savings - platform_cost_usd
    cost_per_blocked: Optional[float] = None  # estimated_savings / blocked_calls
    roi_multiple: Optional[float] = None      # estimated_savings / platform_cost_usd (None when no platform cost)


class SettingResponse(BaseModel):
    key: str
    value: str
    description: Optional[str]
    updated_by: str
    updated_at: datetime

    class Config:
        from_attributes = True


class SettingUpdate(BaseModel):
    value: str = Field(..., min_length=1, max_length=512)
    updated_by: str = Field(default="admin", max_length=128)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _visible_teams(identity: Identity, requested: Optional[str] = None) -> Optional[list[str]]:
    """Team ids an insights query may read (``None`` = every team); see Identity.visible_team_ids."""
    return identity.visible_team_ids(requested)


def _team_sql(teams: Optional[list[str]], column: str = "team_id") -> tuple[str, dict]:
    """Raw-SQL equivalent of :func:`_team_clause`: ``(sql_fragment, bind_params)``."""
    if teams is None:
        return "1=1", {}
    if not teams:
        return "1=0", {}
    params = {f"_t{i}": t for i, t in enumerate(teams)}
    names = ", ".join(f":{k}" for k in params)
    return f"CAST({column} AS TEXT) IN ({names})", params


async def _app_name_map(db: AsyncSession, teams: Optional[list[str]]) -> dict[str, str]:
    """Return {app_id: app_name} for all apps in the given teams (None = all)."""
    rows = await db.execute(
        select(App.id, App.app_name).where(team_scope_clause(App.team_id, teams))
    )
    return {str(r.id): r.app_name for r in rows.all()}


def _confidence_label(r_sq: Optional[Decimal]) -> str:
    if r_sq is None:
        return "low"
    v = float(r_sq)
    if v >= 0.85:
        return "high"
    if v >= 0.60:
        return "medium"
    return "low"


# ── GET /insights/anomalies ────────────────────────────────────────────────────

@insights_router.get("/anomalies", response_model=list[AnomalyResponse])
async def get_anomalies(
    hours: int = Query(24, ge=1, le=168,
                       description="Look-back window in hours"),
    severity: Optional[str] = Query(None,
                                    description="Filter: low|medium|high|critical"),
    include_resolved: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    team_id: Optional[str] = Query(None, description="Restrict to one team (platform admins: all teams when omitted)"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Recent anomaly events for every team the caller can see."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    teams = _visible_teams(identity, team_id)

    q = select(AnomalyEvent).where(
        team_scope_clause(AnomalyEvent.team_id, teams),
        AnomalyEvent.detected_at >= since,
    )
    if severity:
        q = q.where(AnomalyEvent.severity == severity)
    if not include_resolved:
        q = q.where(AnomalyEvent.resolved_at.is_(None))

    q = q.order_by(AnomalyEvent.detected_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(q)).scalars().all()

    app_names = await _app_name_map(db, teams)

    return [
        AnomalyResponse(
            id=r.id,
            app_id=r.app_id,
            app_name=app_names.get(r.app_id) if r.app_id else None,
            team_id=r.team_id,
            metric=r.metric,
            severity=r.severity,
            z_score=float(r.z_score),
            baseline_value=float(r.baseline_value),
            actual_value=float(r.actual_value),
            ai_explanation=r.ai_explanation,
            detected_at=r.detected_at,
            acknowledged_at=r.acknowledged_at,
            resolved_at=r.resolved_at,
        )
        for r in rows
    ]


# ── GET /insights/recommendations ─────────────────────────────────────────────

@insights_router.get("/recommendations", response_model=list[RecommendationResponse])
async def get_recommendations(
    include_dismissed: bool = Query(False),
    min_savings: float = Query(1.0, ge=0,
                               description="Minimum monthly savings to include"),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    team_id: Optional[str] = Query(None, description="Restrict to one team (platform admins: all teams when omitted)"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Model optimization recommendations for every team the caller can see."""
    teams = _visible_teams(identity, team_id)
    q = select(OptimizationRecommendation).where(
        team_scope_clause(OptimizationRecommendation.team_id, teams),
        OptimizationRecommendation.applied_at.is_(None),
        OptimizationRecommendation.estimated_monthly_savings >= Decimal(str(min_savings)),
    )
    if not include_dismissed:
        q = q.where(OptimizationRecommendation.dismissed_at.is_(None))

    q = q.order_by(
        OptimizationRecommendation.estimated_monthly_savings.desc()
    ).limit(limit).offset(offset)

    rows = (await db.execute(q)).scalars().all()
    app_names = await _app_name_map(db, teams)

    return [
        RecommendationResponse(
            id=r.id,
            app_id=r.app_id,
            app_name=app_names.get(r.app_id),
            team_id=r.team_id,
            provider=r.provider,
            current_model=r.current_model,
            suggested_model=r.suggested_model,
            call_volume_basis=r.call_volume_basis,
            estimated_monthly_savings=float(r.estimated_monthly_savings),
            confidence=r.confidence,
            recommendation_text=r.recommendation_text,
            generated_at=r.generated_at,
        )
        for r in rows
    ]


# ── POST /insights/recommendations/{id}/dismiss ────────────────────────────────

@insights_router.post("/recommendations/{rec_id}/dismiss")
async def dismiss_recommendation(
    rec_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    rec = await db.get(OptimizationRecommendation, rec_id)
    if not rec or not identity.can_access_team(rec.team_id):
        raise HTTPException(status_code=404, detail="Recommendation not found")
    rec.dismissed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"dismissed": True}


# ── GET /insights/enforcement-summary ─────────────────────────────────────────

@insights_router.get("/enforcement-summary", response_model=EnforcementSummary)
async def get_enforcement_summary(
    hours: int = Query(24, ge=1, le=720),
    team_id: Optional[str] = Query(None, description="Restrict to one team (platform admins: all teams when omitted)"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Today's policy decision counts and estimated cost savings."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    team_sql, team_params = _team_sql(_visible_teams(identity, team_id))

    result = await db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE decision = 'allow')    AS allowed,
            COUNT(*) FILTER (WHERE decision = 'deny')     AS blocked,
            COUNT(*) FILTER (WHERE decision = 'throttle') AS throttle,
            COUNT(*) FILTER (WHERE decision = 'redirect') AS redirect,
            COUNT(*)                                        AS total,
            COALESCE(SUM(
                CASE WHEN decision = 'deny'
                THEN COALESCE(request_estimated_cost, 0) ELSE 0 END
            ), 0)                                          AS savings
        FROM policy_decisions
        WHERE {team_sql}
          AND decided_at >= :since
    """), {**team_params, "since": sqlite_dt(since)})

    row = result.fetchone()
    period = f"last {hours}h" if hours < 24 else "today"

    blocked = int(row.blocked or 0)
    throttle = int(row.throttle or 0)
    # Plain "allow" decisions are not stored (too high volume, see PolicyDecision),
    # so calls that passed enforcement are derived from metered usage instead.
    allowed = int(row.allowed or 0)
    if allowed == 0:
        calls = await _metered_calls_since(db, since, team_sql, team_params, hours)
        allowed = max(calls - blocked - throttle, 0)

    return EnforcementSummary(
        allowed=allowed,
        blocked=blocked,
        throttle=throttle,
        redirect=int(row.redirect or 0),
        total_decisions=int(row.total or 0) + allowed - int(row.allowed or 0),
        total_savings=float(row.savings or 0),
        period=period,
    )


async def _metered_calls_since(db, since, team_sql, team_params, hours) -> int:
    """Metered call count since ``since`` (hourly rows for recent windows, else daily)."""
    granularity = "hourly" if hours <= 168 else "daily"
    floor = since.replace(minute=0, second=0, microsecond=0)
    if granularity == "daily":
        floor = floor.replace(hour=0)
    res = await db.execute(text(f"""
        SELECT COALESCE(SUM(call_count), 0) AS calls
        FROM usage_aggregates
        WHERE granularity = :g AND period_start >= :since AND {team_sql}
    """), {**team_params, "g": granularity, "since": sqlite_dt(floor)})
    return int(res.scalar() or 0)


# ── GET /insights/ops-kpis ────────────────────────────────────────────────────

@insights_router.get("/ops-kpis", response_model=OpsKpis)
async def get_ops_kpis(
    team_id: Optional[str] = Query(None, description="Restrict to one team"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Operational KPIs for the DevOps view, computed from stored data only."""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    teams = _visible_teams(identity, team_id)
    team_sql, team_params = _team_sql(teams)

    usage = (await db.execute(text(f"""
        SELECT
            COALESCE(SUM(call_count), 0)  AS calls,
            COALESCE(SUM(total_tokens), 0) AS tokens,
            COALESCE(SUM(CASE WHEN period_start >= :hour THEN total_cost ELSE 0 END), 0) AS hour_cost,
            COALESCE(SUM(CASE WHEN duration_ms_sum IS NOT NULL THEN call_count ELSE 0 END), 0) AS timed_calls,
            COALESCE(SUM(duration_ms_sum), 0) AS duration_sum
        FROM usage_aggregates
        WHERE granularity = 'hourly' AND period_start >= :day AND {team_sql}
    """), {**team_params, "day": sqlite_dt(day_start), "hour": sqlite_dt(hour_start)})).one()

    decisions = (await db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE decision = 'deny')     AS blocked,
            COUNT(*) FILTER (WHERE decision = 'throttle') AS throttled
        FROM policy_decisions
        WHERE decided_at >= :day AND {team_sql}
    """), {**team_params, "day": sqlite_dt(day_start)})).one()

    calls = int(usage.calls or 0)
    timed = int(usage.timed_calls or 0)
    return OpsKpis(
        window_start=day_start,
        cost_this_hour=float(usage.hour_cost or 0),
        blocked_today=int(decisions.blocked or 0),
        throttled_today=int(decisions.throttled or 0),
        calls_today=calls,
        tokens_per_call=round(float(usage.tokens or 0) / calls, 1) if calls else None,
        avg_latency_ms=round(float(usage.duration_sum or 0) / timed, 1) if timed else None,
        latency_samples=timed,
    )


# ── GET /insights/forecast ────────────────────────────────────────────────────

@insights_router.get("/forecast", response_model=list[ForecastResponse])
async def get_forecast(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Latest spend forecasts. Returns per-team forecasts the identity can see."""
    # Get latest forecast per team using a subquery
    subq = (
        select(
            SpendForecast.team_id,
            func.max(SpendForecast.computed_at).label("latest"),
        )
        .group_by(SpendForecast.team_id)
        .subquery()
    )

    q = (
        select(SpendForecast)
        .join(
            subq,
            (SpendForecast.team_id == subq.c.team_id)
            & (SpendForecast.computed_at == subq.c.latest),
        )
        .order_by(SpendForecast.forecast_eom.desc())
    )

    # Scope to requesting team unless platform admin
    q = q.where(team_scope_clause(SpendForecast.team_id, _visible_teams(identity)))

    rows = (await db.execute(q)).scalars().all()

    return [
        ForecastResponse(
            team_id=r.team_id,
            period_label=r.period_label,
            mtd_actual=float(r.mtd_actual),
            forecast_eom=float(r.forecast_eom),
            forecast_eoq=float(r.forecast_eoq) if r.forecast_eoq else None,
            forecast_eoy=float(r.forecast_eoy) if r.forecast_eoy else None,
            trend_pct=float(r.trend_pct) if r.trend_pct else None,
            r_squared=float(r.r_squared) if r.r_squared else None,
            confidence_label=_confidence_label(r.r_squared),
            computed_at=r.computed_at,
        )
        for r in rows
    ]


# ── GET /reports/roi ───────────────────────────────────────────────────────────

@reports_router.get("/roi", response_model=RoiResponse)
async def get_roi(
    days: int = Query(30, ge=1, le=365),
    team_id: Optional[str] = Query(None, description="Restrict to one team (platform admins: all teams when omitted)"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """
    ROI from Modus policy enforcement.
    Blocked calls × estimated cost per call = savings.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    period = f"last {days}d" if days != 30 else "MTD"
    team_sql, team_params = _team_sql(_visible_teams(identity, team_id))

    # Savings from blocked calls
    roi_result = await db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE decision = 'deny')  AS blocked_calls,
            COALESCE(SUM(
                CASE WHEN decision = 'deny'
                THEN COALESCE(request_estimated_cost, 0) ELSE 0 END
            ), 0) AS estimated_savings
        FROM policy_decisions
        WHERE {team_sql}
          AND decided_at >= :since
    """), {**team_params, "since": sqlite_dt(since)})
    roi_row = roi_result.fetchone()

    # Top policy by block count
    top_policy_result = await db.execute(text(f"""
        SELECT policy_id, COUNT(*) AS blocks
        FROM policy_decisions
        WHERE {team_sql}
          AND decided_at >= :since
          AND decision = 'deny'
          AND policy_id IS NOT NULL
        GROUP BY policy_id
        ORDER BY blocks DESC
        LIMIT 1
    """), {**team_params, "since": sqlite_dt(since)})
    top_row = top_policy_result.fetchone()

    top_policy_name = None
    top_policy_blocks = None
    if top_row:
        top_policy_blocks = int(top_row.blocks)
        # Look up policy name
        pol_result = await db.execute(text("""
            SELECT name FROM governance_policies WHERE id = :pid
        """), {"pid": str(top_row.policy_id)})
        pol_row = pol_result.fetchone()
        if pol_row:
            top_policy_name = pol_row.name

    blocked_calls = int(roi_row.blocked_calls or 0)
    savings = float(roi_row.estimated_savings or 0)
    platform_cost = 0.0
    return RoiResponse(
        period=period,
        blocked_calls=blocked_calls,
        estimated_savings=savings,
        top_policy=top_policy_name,
        top_policy_blocks=top_policy_blocks,
        platform_cost_usd=platform_cost,
        net_savings=savings - platform_cost,
        cost_per_blocked=(savings / blocked_calls) if blocked_calls else None,
        roi_multiple=(savings / platform_cost) if platform_cost else None,
    )


# ── GET /reports/chargeback ────────────────────────────────────────────────────

@reports_router.get("/chargeback")
async def get_chargeback(
    period: Optional[str] = Query(None,
                                   description="Period: YYYY-MM, or omit for last 30d"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Chargeback data by team. Returns JSON; use ?format=csv for CSV export."""
    now = datetime.now(timezone.utc)
    if period:
        try:
            year, month = int(period[:4]), int(period[5:7])
            period_start = datetime(year, month, 1, tzinfo=timezone.utc)
            period_end = (
                datetime(year, month % 12 + 1, 1, tzinfo=timezone.utc)
                if month < 12
                else datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            )
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="period must be YYYY-MM")
    else:
        period_start = now - timedelta(days=30)
        period_end = now

    team_sql, team_params = _team_sql(_visible_teams(identity), "ua.team_id")

    q = text(f"""
        SELECT
            t.slug               AS team_slug,
            t.name               AS team_name,
            a.app_name,
            ua.provider,
            ua.model,
            SUM(ua.call_count)   AS calls,
            SUM(ua.total_tokens) AS tokens,
            SUM(ua.total_cost)   AS cost
        FROM usage_aggregates ua
        JOIN apps    a ON a.id = ua.app_id
        JOIN teams   t ON t.id = ua.team_id
        WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start
          AND ua.period_start < :end
          AND {team_sql}
        GROUP BY t.slug, t.name, a.app_name, ua.provider, ua.model
        ORDER BY SUM(ua.total_cost) DESC
    """)

    result = await db.execute(q, {
        "start": sqlite_dt(period_start),
        "end": sqlite_dt(period_end),
        **team_params,
    })
    rows = result.all()

    return [
        {
            "team_slug": r.team_slug,
            "team_name": r.team_name,
            "app_name": r.app_name,
            "provider": r.provider,
            "model": r.model,
            "calls": int(r.calls or 0),
            "tokens": int(r.tokens or 0),
            "cost": str(r.cost or 0),
        }
        for r in rows
    ]


# ── GET /admin/settings ────────────────────────────────────────────────────────

@admin_router.get("/settings", response_model=list[SettingResponse])
async def list_settings(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """List all system settings. Platform admin only."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    rows = (await db.execute(
        select(SystemSetting).order_by(SystemSetting.key)
    )).scalars().all()

    # Merge with defaults — show settings that exist in DB, plus any defaults
    # not yet in DB (with empty updated_by to signal they're defaults)
    existing_keys = {r.key for r in rows}
    result = list(rows)

    for key, default_val in _DEFAULTS.items():
        if key not in existing_keys:
            result.append(SystemSetting(
                key=key,
                value=default_val,
                description=None,
                updated_by="default",
                updated_at=datetime.now(timezone.utc),
            ))

    return [
        SettingResponse(
            key=r.key,
            value=r.value,
            description=r.description,
            updated_by=r.updated_by,
            updated_at=r.updated_at,
        )
        for r in sorted(result, key=lambda x: x.key)
    ]


# ── PUT /admin/settings/{key} ──────────────────────────────────────────────────

@admin_router.put("/settings/{key}", response_model=SettingResponse)
async def update_setting(
    key: str,
    payload: SettingUpdate,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """Update a single system setting. Takes effect within 30 seconds."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    if key not in _DEFAULTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown setting key '{key}'. Valid keys: {sorted(_DEFAULTS.keys())}",
        )

    # Validate numeric settings
    numeric_keys = {k for k in _DEFAULTS if "seconds" in k or "days" in k}
    if key in numeric_keys:
        try:
            val = int(payload.value)
            if val < 1:
                raise ValueError("Must be positive")
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Setting '{key}' must be a positive integer"
            )

    # Boolean settings accept exactly "true" / "false" (normalised).
    if _DEFAULTS[key] in ("true", "false"):
        normalised = str(payload.value).strip().lower()
        if normalised not in ("true", "false"):
            raise HTTPException(
                status_code=422,
                detail=f"Setting '{key}' must be 'true' or 'false'",
            )
        payload.value = normalised

    now = datetime.now(timezone.utc)
    existing = await db.get(SystemSetting, key)
    if existing:
        existing.value = payload.value
        existing.updated_by = payload.updated_by
        existing.updated_at = now
        setting = existing
    else:
        setting = SystemSetting(
            key=key,
            value=payload.value,
            updated_by=payload.updated_by,
            updated_at=now,
        )
        db.add(setting)

    await db.commit()
    await db.refresh(setting)

    logger.info(
        "System setting updated",
        extra={"key": key, "value": payload.value, "by": payload.updated_by},
    )

    return SettingResponse(
        key=setting.key,
        value=setting.value,
        description=setting.description,
        updated_by=setting.updated_by,
        updated_at=setting.updated_at,
    )


# ── POST /admin/tasks/{task}/trigger ──────────────────────────────────────────

_TASK_MAP = {
    "anomaly-scan":      run_anomaly_scan,
    "forecast":          run_forecast_update,
    "recommendations":   run_recommendation_refresh,
}


# ── POST /insights/nl-query — Natural language cost query (Phase 4e) ─────────

class NlQueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)


class NlQueryResponse(BaseModel):
    question: str
    answer: str
    data: Optional[list[dict]] = None
    sql_used: Optional[str] = None


@insights_router.post("/nl-query", response_model=NlQueryResponse)
async def natural_language_query(
    body: NlQueryRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
):
    """
    Translate a natural language question about AI spend into a SQL query,
    execute it against usage data, and return a human-readable answer.

    Gated by feature_nl_query setting. Uses the configured summary AI agent
    (Claude Haiku by default) to generate read-only SQL.

    Examples:
      - "What was our total spend last week?"
      - "Which app costs the most this month?"
      - "Show me daily costs for the past 7 days"
    """
    from orchestrator.core.config import settings as _s
    if not _s.feature_nl_query:
        raise HTTPException(
            status_code=403,
            detail="Natural language queries are not enabled. Set MODUS_FEATURE_NL_QUERY=true.",
        )

    from orchestrator.core.insights_engine import _ai_explain

    # Build schema context for the AI
    schema_hint = (
        "Tables available (read-only):\n"
        "- usage_aggregates: id, app_id, team_id, provider, model, resource_type, "
        "granularity ('hourly'|'daily'), period_start, period_end, "
        "call_count, input_tokens, output_tokens, total_tokens, total_cost, avg_duration_ms\n"
        "- apps: id, team_id, app_id (slug), app_name, environment\n"
        "- teams: id, slug, name\n"
        "All monetary values are in USD. Timestamps are UTC.\n"
        "The current team_id is: " + str(identity.team_id) + "\n"
        "Always filter by team_id = the current team_id for security.\n"
        "Use granularity='daily' for queries spanning days or longer.\n"
        "Use granularity='hourly' for intra-day queries.\n"
        "Return ONLY a SELECT statement. No INSERT/UPDATE/DELETE/DROP/ALTER.\n"
        "Do not use CTEs or subqueries if a simple query will do.\n"
        "Return the SQL only, no markdown, no explanation."
    )

    system_prompt = (
        "You are a SQL assistant for an AI cost tracking database. "
        "Given a user question, generate a single read-only SELECT query. "
        "Here is the schema:\n" + schema_hint
    )

    # Generate SQL via AI
    generated_sql = await _ai_explain(system_prompt, body.question, max_tokens=500)
    if not generated_sql:
        raise HTTPException(500, "Failed to generate query. Check AI engine configuration.")

    # Clean up — strip markdown fences if present
    sql = generated_sql.strip()
    if sql.startswith("```"):
        sql = sql.split("\n", 1)[-1] if "\n" in sql else sql[3:]
    if sql.endswith("```"):
        sql = sql[:-3].strip()
    sql = sql.strip().rstrip(";")

    # Safety: reject anything that isn't a SELECT
    sql_upper = sql.upper().lstrip()
    if not sql_upper.startswith("SELECT"):
        raise HTTPException(400, "Generated query was not a SELECT statement.")

    dangerous = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "GRANT", "REVOKE"}
    tokens = sql_upper.split()
    if any(t in dangerous for t in tokens):
        raise HTTPException(400, "Generated query contains disallowed statements.")

    # Execute read-only with row limit inside a savepoint for safety
    try:
        async with db.begin_nested():
            result = await db.execute(text(sql + " LIMIT 100"))
            rows = result.mappings().all()
            data = [dict(r) for r in rows]
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("NL query SQL execution failed: %s", exc, extra={"sql": sql})
        raise HTTPException(400, "Query execution failed. Please rephrase your question.")

    # Generate human-readable answer from the data
    import json as _json
    answer_prompt = (
        "You are a concise analytics assistant. Given a user's question and query results, "
        "provide a clear 1-3 sentence answer. Include key numbers. "
        "If the data is empty, say so. Format currency as USD."
    )
    data_summary = _json.dumps(data[:20], default=str)[:2000]
    answer_context = f"Question: {body.question}\nData ({len(data)} rows): {data_summary}"
    answer = await _ai_explain(answer_prompt, answer_context, max_tokens=300)

    # Serialize Decimal/datetime values in data for JSON response
    def _serialize(v):
        if hasattr(v, "isoformat"):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        return v

    clean_data = [{k: _serialize(v) for k, v in row.items()} for row in data]

    return NlQueryResponse(
        question=body.question,
        answer=answer or "Query returned data but summary generation failed.",
        data=clean_data[:100],
        sql_used=sql,
    )


@admin_router.post("/tasks/{task}/trigger")
async def trigger_task(
    task: str,
    identity: Identity = Depends(get_identity),
):
    """Immediately trigger a background task. Returns immediately; runs async."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    fn = _TASK_MAP.get(task)
    if not fn:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown task '{task}'. Valid: {sorted(_TASK_MAP.keys())}",
        )

    import asyncio
    asyncio.create_task(fn())
    logger.info("Task triggered manually", extra={"task": task, "by": identity.team_id})

    return {"triggered": task, "status": "running"}
