"""
Modus — Dashboard Data Router
======================================
All read endpoints that power the dashboard UI.

Reads primarily from usage_aggregates for performance.
Falls back to usage_records for fine-grained queries within 24h windows.

GET /api/v1/dashboard/summary          — top-level KPI cards
GET /api/v1/dashboard/cost-over-time   — chart data: cost by day/hour
GET /api/v1/dashboard/by-provider      — breakdown by provider
GET /api/v1/dashboard/by-app           — breakdown by app
GET /api/v1/dashboard/by-team          — breakdown by team (platform admin only)
GET /api/v1/dashboard/top-models       — most expensive models
GET /api/v1/dashboard/recent-alerts    — recent threshold breaches
GET /api/v1/dashboard/app-status       — agent online/offline status
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query


from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import Alert, App, Team, UsageAggregate, AgentHeartbeat
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard")


# ── Response schemas ───────────────────────────────────────────────────────────

class KpiSummary(BaseModel):
    total_cost_today: Decimal
    total_cost_7d: Decimal
    total_cost_30d: Decimal
    total_cost_mtd: Decimal              # month-to-date (1st of month → now)
    total_tokens_today: int
    total_calls_today: int
    total_calls_mtd: int                 # month-to-date call count
    active_apps: int
    online_agents: int
    active_teams: int                    # teams with any AI activity in window
    cost_delta_pct_7d: Optional[float]    # % change vs prior 7d
    cost_per_1k_tokens: Optional[float]  # blended cost per 1K tokens, 30d


class TimeSeriesPoint(BaseModel):
    period: datetime
    cost: Decimal
    tokens: int
    calls: int


class ProviderBreakdown(BaseModel):
    provider: str
    cost: Decimal
    tokens: int
    calls: int
    cost_pct: float


class AppBreakdown(BaseModel):
    app_id: str
    app_name: str
    team_slug: str
    environment: str
    cost: Decimal
    tokens: int
    calls: int


class TeamBreakdown(BaseModel):
    team_slug: str
    team_name: str
    cost: Decimal
    tokens: int
    calls: int
    app_count: int


class ModelBreakdown(BaseModel):
    provider: str
    model: str
    cost: Decimal
    calls: int
    avg_input_tokens: Optional[int]
    avg_output_tokens: Optional[int]


class AlertSummary(BaseModel):
    id: str
    severity: str
    metric: str
    threshold_value: Decimal
    actual_value: Decimal
    app_id: Optional[str]
    team_id: str
    fired_at: datetime
    acknowledged: bool
    notification_sent: Optional[bool] = None
    notification_result: Optional[dict] = None


class AppStatus(BaseModel):
    app_uuid: str
    app_id: str
    app_name: str
    team_slug: str
    environment: str
    online: bool
    last_seen_at: Optional[datetime]
    agent_version: Optional[str]
    instrumented_providers: Optional[list[str]]


class ExecutiveCharts(BaseModel):
    savings_over_time: list[TimeSeriesPoint]
    enforcement_mix: dict[str, int]
    provider_allocation: list[ProviderBreakdown]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _window(days: int) -> datetime:
    return _now() - timedelta(days=days)


def _apply_team_scope(query, identity: Identity, team_model):
    """Apply team visibility scoping to a query."""
    if not identity.is_platform_admin and identity.team_ids:
        return query.where(team_model.id.in_(identity.team_ids))
    return query


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/summary", response_model=KpiSummary)
async def dashboard_summary(
    team_id: Optional[str] = Query(None, description="Filter to specific team UUID"),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> KpiSummary:
    """KPI cards for the dashboard header."""

    if team_id:
        identity.assert_team_access(team_id)

    today_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    d7_start = _window(7)
    d30_start = _window(30)
    prior_7d_start = _window(14)

    # Base filter — daily rows only. Hourly and daily rows hold the same
    # usage (both are updated in the same ingest transaction, see
    # core/usage_rollup.py), so mixing them would double count; daily rows
    # are current to the last ingested batch, including today's.
    def _agg_filter(q, since: datetime, until: Optional[datetime] = None):
        q = q.where(
            UsageAggregate.period_start >= since,
            UsageAggregate.granularity == "daily",
        )
        if until is not None:
            q = q.where(UsageAggregate.period_start < until)
        if team_id:
            q = q.where(UsageAggregate.team_id == team_id)
        elif not identity.is_platform_admin and identity.team_ids:
            q = q.where(UsageAggregate.team_id.in_(identity.team_ids))
        return q

    async def _sum_cost(since: datetime, until: Optional[datetime] = None) -> Decimal:
        r = await db.execute(
            _agg_filter(
                select(func.coalesce(func.sum(UsageAggregate.total_cost), 0)),
                since,
                until,
            )
        )
        return r.scalar() or Decimal("0")

    async def _sum_tokens(since: datetime) -> int:
        r = await db.execute(
            _agg_filter(
                select(func.coalesce(func.sum(UsageAggregate.total_tokens), 0)),
                since,
            )
        )
        # SUM(bigint) is NUMERIC on PostgreSQL (Decimal); counts are ints.
        return int(r.scalar() or 0)

    async def _sum_calls(since: datetime) -> int:
        r = await db.execute(
            _agg_filter(
                select(func.coalesce(func.sum(UsageAggregate.call_count), 0)),
                since,
            )
        )
        return int(r.scalar() or 0)

    # MTD = 1st of current month → now
    mtd_start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    cost_today = await _sum_cost(today_start)
    cost_7d = await _sum_cost(d7_start)
    cost_30d = await _sum_cost(d30_start)
    cost_mtd = await _sum_cost(mtd_start)
    # The 7 days before the current 7-day window — not the 14-day total.
    cost_prior_7d = await _sum_cost(prior_7d_start, d7_start)
    tokens_today = await _sum_tokens(today_start)
    tokens_30d = await _sum_tokens(d30_start)
    calls_today = await _sum_calls(today_start)
    calls_mtd = await _sum_calls(mtd_start)

    # Cost delta
    cost_delta = None
    if cost_prior_7d and cost_prior_7d > 0:
        cost_delta = round(float((cost_7d - cost_prior_7d) / cost_prior_7d * 100), 1)

    # Cost per 1K tokens (30d, blended)
    cost_per_1k = None
    if tokens_30d and tokens_30d > 0:
        cost_per_1k = round(float(cost_30d) / (tokens_30d / 1000), 4)

    # Active apps
    app_q = select(func.count(App.id)).where(
        App.is_active == True, App.deleted_at.is_(None)
    )
    if team_id:
        app_q = app_q.where(App.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        app_q = app_q.where(App.team_id.in_(identity.team_ids))
    active_apps = (await db.execute(app_q)).scalar() or 0

    # Online agents — heartbeat within stale window
    from orchestrator.core.config import settings
    stale_cutoff = _now() - timedelta(minutes=settings.heartbeat_stale_minutes)
    online_q = select(func.count(App.id)).where(
        App.is_active == True,
        App.deleted_at.is_(None),
        App.last_seen_at >= stale_cutoff,
    )
    if team_id:
        online_q = online_q.where(App.team_id == team_id)
    online_agents = (await db.execute(online_q)).scalar() or 0

    # Active teams — teams with any usage in the 30-day window
    active_teams_q = _agg_filter(
        select(func.count(func.distinct(UsageAggregate.team_id))),
        d30_start,
    )
    active_teams = (await db.execute(active_teams_q)).scalar() or 0

    return KpiSummary(
        total_cost_today=cost_today,
        total_cost_7d=cost_7d,
        total_cost_30d=cost_30d,
        total_cost_mtd=cost_mtd,
        total_tokens_today=tokens_today,
        total_calls_today=calls_today,
        total_calls_mtd=calls_mtd,
        active_apps=active_apps,
        online_agents=online_agents,
        active_teams=active_teams,
        cost_delta_pct_7d=cost_delta,
        cost_per_1k_tokens=cost_per_1k,
    )


@router.get("/cost-over-time", response_model=list[TimeSeriesPoint])
async def cost_over_time(
    days: int = Query(7, ge=1, le=90),
    granularity: str = Query("daily", pattern="^(hourly|daily)$"),
    team_id: Optional[str] = None,
    app_id: Optional[str] = None,
    provider: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[TimeSeriesPoint]:
    if team_id:
        identity.assert_team_access(team_id)

    since = _window(days)
    day_col = func.date(UsageAggregate.period_start).label("day")
    q = select(
        day_col,
        func.sum(UsageAggregate.total_cost).label("cost"),
        func.sum(UsageAggregate.total_tokens).label("tokens"),
        func.sum(UsageAggregate.call_count).label("calls"),
    ).where(
        UsageAggregate.period_start >= since,
        UsageAggregate.granularity == granularity,
    )

    if team_id:
        q = q.where(UsageAggregate.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(UsageAggregate.team_id.in_(identity.team_ids))
    if app_id:
        q = q.where(UsageAggregate.app_id == app_id)
    if provider:
        q = q.where(UsageAggregate.provider == provider)

    q = q.group_by(day_col).order_by(day_col)

    result = await db.execute(q)
    return [
        TimeSeriesPoint(
            period=row.day,
            cost=row.cost or Decimal("0"),
            tokens=row.tokens or 0,
            calls=row.calls or 0,
        )
        for row in result.all()
    ]


@router.get(
    "/by-provider", response_model=list[ProviderBreakdown],
)
async def by_provider(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[ProviderBreakdown]:
    if team_id:
        identity.assert_team_access(team_id)

    since = _window(days)
    q = select(
        UsageAggregate.provider,
        func.sum(UsageAggregate.total_cost).label("cost"),
        func.sum(UsageAggregate.total_tokens).label("tokens"),
        func.sum(UsageAggregate.call_count).label("calls"),
    ).where(
        UsageAggregate.period_start >= since,
        UsageAggregate.granularity == "daily",
    )

    if team_id:
        q = q.where(UsageAggregate.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(UsageAggregate.team_id.in_(identity.team_ids))

    q = q.group_by(UsageAggregate.provider).order_by(func.sum(UsageAggregate.total_cost).desc())

    rows = (await db.execute(q)).all()
    total = sum(r.cost or Decimal("0") for r in rows)

    return [
        ProviderBreakdown(
            provider=r.provider,
            cost=r.cost or Decimal("0"),
            tokens=r.tokens or 0,
            calls=r.calls or 0,
            cost_pct=round(float((r.cost or 0) / total * 100), 1) if total else 0.0,
        )
        for r in rows
    ]


@router.get(
    "/by-app", response_model=list[AppBreakdown],
)
async def by_app(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[AppBreakdown]:
    if team_id:
        identity.assert_team_access(team_id)

    since = _window(days)
    q = (
        select(
            App.app_id,
            App.app_name,
            App.environment,
            Team.slug.label("team_slug"),
            func.sum(UsageAggregate.total_cost).label("cost"),
            func.sum(UsageAggregate.total_tokens).label("tokens"),
            func.sum(UsageAggregate.call_count).label("calls"),
        )
        .join(App, UsageAggregate.app_id == App.id)
        .join(Team, App.team_id == Team.id)
        .where(
            UsageAggregate.period_start >= since,
            UsageAggregate.granularity == "daily",
        )
    )

    if team_id:
        q = q.where(UsageAggregate.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(UsageAggregate.team_id.in_(identity.team_ids))

    q = (
        q.group_by(App.app_id, App.app_name, App.environment, Team.slug)
        .order_by(func.sum(UsageAggregate.total_cost).desc())
        .limit(limit)
    )

    rows = (await db.execute(q)).all()
    return [
        AppBreakdown(
            app_id=r.app_id, app_name=r.app_name, team_slug=r.team_slug,
            environment=r.environment,
            cost=r.cost or Decimal("0"), tokens=r.tokens or 0, calls=r.calls or 0,
        )
        for r in rows
    ]


@router.get(
    "/by-team", response_model=list[TeamBreakdown],
)
async def by_team(
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[TeamBreakdown]:
    since = _window(days)
    q = (
        select(
            Team.slug,
            Team.name,
            func.sum(UsageAggregate.total_cost).label("cost"),
            func.sum(UsageAggregate.total_tokens).label("tokens"),
            func.sum(UsageAggregate.call_count).label("calls"),
            func.count(func.distinct(UsageAggregate.app_id)).label("app_count"),
        )
        .join(Team, UsageAggregate.team_id == Team.id)
        .where(
            UsageAggregate.period_start >= since,
            UsageAggregate.granularity == "daily",
        )
    )

    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(UsageAggregate.team_id.in_(identity.team_ids))

    q = q.group_by(Team.slug, Team.name).order_by(
        func.sum(UsageAggregate.total_cost).desc()
    ).limit(limit).offset(offset)

    rows = (await db.execute(q)).all()
    return [
        TeamBreakdown(
            team_slug=r.slug, team_name=r.name,
            cost=r.cost or Decimal("0"), tokens=r.tokens or 0,
            calls=r.calls or 0, app_count=r.app_count or 0,
        )
        for r in rows
    ]


@router.get("/top-models", response_model=list[ModelBreakdown])
async def top_models(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = None,
    app_id: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[ModelBreakdown]:
    if team_id:
        identity.assert_team_access(team_id)

    since = _window(days)
    q = select(
        UsageAggregate.provider,
        UsageAggregate.model,
        func.sum(UsageAggregate.total_cost).label("cost"),
        func.sum(UsageAggregate.call_count).label("calls"),
        func.sum(UsageAggregate.input_tokens).label("input_tokens"),
        func.sum(UsageAggregate.output_tokens).label("output_tokens"),
    ).where(
        UsageAggregate.period_start >= since,
        UsageAggregate.granularity == "daily",
        UsageAggregate.model.isnot(None),
    )

    if team_id:
        q = q.where(UsageAggregate.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(UsageAggregate.team_id.in_(identity.team_ids))

    if app_id:
        # Resolve external app_id → internal app uuid
        app_row = (await db.execute(
            select(App.id).where(App.app_id == app_id)
        )).scalar_one_or_none()
        if app_row is not None:
            q = q.where(UsageAggregate.app_id == app_row)

    q = (
        q.group_by(UsageAggregate.provider, UsageAggregate.model)
        .order_by(func.sum(UsageAggregate.total_cost).desc())
        .limit(limit)
    )

    rows = (await db.execute(q)).all()
    return [
        ModelBreakdown(
            provider=r.provider,
            model=r.model or "unknown",
            cost=r.cost or Decimal("0"),
            calls=r.calls or 0,
            avg_input_tokens=int(r.input_tokens / r.calls) if r.calls else None,
            avg_output_tokens=int(r.output_tokens / r.calls) if r.calls else None,
        )
        for r in rows
    ]


@router.get("/recent-alerts", response_model=list[AlertSummary])
async def recent_alerts(
    limit: int = Query(20, ge=1, le=100),
    unacknowledged_only: bool = False,
    app_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[AlertSummary]:
    q = select(Alert).where(Alert.fired_at >= _window(7))

    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(Alert.team_id.in_(identity.team_ids))
    if unacknowledged_only:
        q = q.where(Alert.acknowledged_at.is_(None))
    if app_id:
        q = q.where(Alert.app_id == app_id)

    q = q.order_by(Alert.fired_at.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    return [
        AlertSummary(
            id=str(a.id), severity=a.severity, metric=a.metric,
            threshold_value=a.threshold_value, actual_value=a.actual_value,
            app_id=str(a.app_id) if a.app_id else None,
            team_id=str(a.team_id), fired_at=a.fired_at,
            acknowledged=a.acknowledged_at is not None,
            notification_sent=a.notification_sent,
            notification_result=a.notification_result,
        )
        for a in rows
    ]


@router.get(
    "/app-status", response_model=list[AppStatus],
)
async def app_status(
    team_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[AppStatus]:
    from orchestrator.core.config import settings as cfg
    stale_cutoff = _now() - timedelta(minutes=cfg.heartbeat_stale_minutes)

    q = (
        select(App, Team.slug)
        .join(Team, App.team_id == Team.id)
        .where(App.is_active == True, App.deleted_at.is_(None))
    )

    if team_id:
        identity.assert_team_access(team_id)
        q = q.where(App.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(App.team_id.in_(identity.team_ids))

    q = q.order_by(
        func.coalesce(App.last_seen_at, datetime(1970, 1, 1, tzinfo=timezone.utc)).desc()
    )
    rows = (await db.execute(q)).all()

    # Get latest heartbeat per app for instrumented_providers
    app_ids = [str(app.id) for app, _ in rows]
    heartbeat_map: dict[str, AgentHeartbeat] = {}
    if app_ids:
        hb_q = (
            select(AgentHeartbeat)
            .where(AgentHeartbeat.app_id.in_(app_ids))
            .order_by(AgentHeartbeat.received_at.desc())
        )
        hb_rows = (await db.execute(hb_q)).scalars().all()
        for hb in hb_rows:
            if hb.app_id not in heartbeat_map:
                heartbeat_map[hb.app_id] = hb

    return [
        AppStatus(
            app_uuid=str(app.id),
            app_id=app.app_id,
            app_name=app.app_name,
            team_slug=team_slug,
            environment=app.environment,
            online=(
                app.last_seen_at is not None
                and (
                    app.last_seen_at.replace(tzinfo=timezone.utc)
                    if app.last_seen_at.tzinfo is None
                    else app.last_seen_at
                ) >= stale_cutoff
            ),
            last_seen_at=app.last_seen_at,
            agent_version=app.agent_version,
            instrumented_providers=(
                heartbeat_map[str(app.id)].instrumented_providers
                if str(app.id) in heartbeat_map else None
            ),
        )
        for app, team_slug in rows
    ]


@router.get(
    "/executive-charts", response_model=ExecutiveCharts,
)
async def executive_charts(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> ExecutiveCharts:
    """Actionable charts for the executive view."""
    if team_id:
        identity.assert_team_access(team_id)

    since = _window(days)

    # 1. Savings over time (deny/throttle prevention)
    from orchestrator.db.models import PolicyDecision
    from orchestrator.core.config import settings as _cfg
    _day_trunc = (
        func.strftime("%Y-%m-%d 00:00:00", PolicyDecision.decided_at)
        if _cfg.is_sqlite
        else func.date_trunc("day", PolicyDecision.decided_at)
    )
    savings_q = select(
        _day_trunc.label("day"),
        func.sum(PolicyDecision.request_estimated_cost).label("saved_cost")
    ).where(
        PolicyDecision.decided_at >= since,
        PolicyDecision.decision.in_(["deny", "throttle"])
    )
    if team_id:
        savings_q = savings_q.where(PolicyDecision.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        savings_q = savings_q.where(PolicyDecision.team_id.in_(identity.team_ids))

    savings_q = savings_q.group_by(_day_trunc).order_by("day")
    savings_rows = (await db.execute(savings_q)).all()

    savings_over_time = [
        TimeSeriesPoint(period=r.day, cost=r.saved_cost or Decimal("0"), tokens=0, calls=0)
        for r in savings_rows
    ]

    # 2. Enforcement mix
    mix_q = select(
        PolicyDecision.decision,
        func.count(PolicyDecision.id).label("count")
    ).where(PolicyDecision.decided_at >= since)
    if team_id:
        mix_q = mix_q.where(PolicyDecision.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        mix_q = mix_q.where(PolicyDecision.team_id.in_(identity.team_ids))

    mix_q = mix_q.group_by(PolicyDecision.decision)
    mix_rows = (await db.execute(mix_q)).all()
    enforcement_mix = {r.decision: r.count for r in mix_rows}

    # 3. Provider allocation (already exists, we reuse the logic)
    provider_alloc = await by_provider(days=days, team_id=team_id, identity=identity, db=db)

    return ExecutiveCharts(
        savings_over_time=savings_over_time,
        enforcement_mix=enforcement_mix,
        provider_allocation=provider_alloc
    )
