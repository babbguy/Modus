"""
Modus Conductor — Dashboard Data API
=============================================
All read endpoints that power the dashboard UI.

CRITICAL: These endpoints must return the EXACT same response schemas as
the Orchestrator's dashboard_data.py. The dashboard doesn't care whether
it's talking to an Orchestrator or a Conductor — same contract, same fields,
same types, same sort order.

The difference: the Conductor queries its own ConductorAggregate table
which contains data from ALL Orchestrators across the org, giving the
dashboard a unified, org-wide view.

Additional field: data_completeness_pct is included in relevant responses
so the dashboard can show a warning when data is incomplete.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.core.auth import Identity, get_identity
from conductor.core.cache import CacheTier, cache
from conductor.db.models import (
    AlertCache,
    AppCache,
    ConductorAggregate,
    PolicyDecisionCache,
    ReconciliationSnapshot,
    TeamCache,
)
from conductor.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard")


# ── Response schemas — IDENTICAL to Orchestrator's dashboard_data.py ──────────


class KpiSummary(BaseModel):
    total_cost_today: Decimal
    total_cost_7d: Decimal
    total_cost_30d: Decimal
    total_cost_mtd: Decimal
    total_tokens_today: int
    total_calls_today: int
    total_calls_mtd: int
    active_apps: int
    online_agents: int
    active_teams: int
    cost_delta_pct_7d: Optional[float]
    cost_per_1k_tokens: Optional[float]
    # Conductor-specific: data completeness info
    data_completeness_pct: Optional[float] = None
    data_status: Optional[str] = None


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


def _apply_team_scope(query, identity: Identity, team_id_col):
    """Apply team visibility scoping — non-admins see only their teams."""
    if not identity.is_platform_admin and identity.team_ids:
        return query.where(team_id_col.in_(identity.team_ids))
    return query


async def _get_completeness(db: AsyncSession) -> tuple[Optional[float], Optional[str]]:
    """Get latest data completeness from reconciliation snapshot."""
    result = await db.execute(
        select(ReconciliationSnapshot)
        .order_by(ReconciliationSnapshot.snapshot_at.desc())
        .limit(1)
    )
    snap = result.scalar_one_or_none()
    if snap:
        return float(snap.completeness_pct), snap.status
    return None, None


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("/summary", response_model=KpiSummary, tags=["dashboard"])
async def dashboard_summary(
    team_id: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Top-level KPI cards — org-wide or filtered by team."""
    # Check cache
    cache_key = f"summary:{team_id or 'all'}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    if team_id:
        identity.assert_team_access(team_id)

    now = _now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    d7_start = _window(7)
    d30_start = _window(30)
    mtd_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prior_7d_start = d7_start - timedelta(days=7)

    CA = ConductorAggregate
    base_filter = [CA.granularity == "daily"]
    if team_id:
        base_filter.append(CA.team_id == team_id)

    # Cost today
    q = select(func.coalesce(func.sum(CA.total_cost), 0)).where(
        *base_filter, CA.period_start >= today_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    cost_today = (await db.execute(q)).scalar() or Decimal("0")

    # Cost 7d
    q = select(func.coalesce(func.sum(CA.total_cost), 0)).where(
        *base_filter, CA.period_start >= d7_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    cost_7d = (await db.execute(q)).scalar() or Decimal("0")

    # Cost 30d
    q = select(func.coalesce(func.sum(CA.total_cost), 0)).where(
        *base_filter, CA.period_start >= d30_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    cost_30d = (await db.execute(q)).scalar() or Decimal("0")

    # Cost MTD
    q = select(func.coalesce(func.sum(CA.total_cost), 0)).where(
        *base_filter, CA.period_start >= mtd_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    cost_mtd = (await db.execute(q)).scalar() or Decimal("0")

    # Prior 7d cost (for delta)
    q = select(func.coalesce(func.sum(CA.total_cost), 0)).where(
        *base_filter, CA.period_start >= prior_7d_start, CA.period_start < d7_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    cost_prior_7d = (await db.execute(q)).scalar() or Decimal("0")

    # Tokens today
    q = select(func.coalesce(func.sum(CA.total_tokens), 0)).where(
        *base_filter, CA.period_start >= today_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    tokens_today = (await db.execute(q)).scalar() or 0

    # Calls today
    q = select(func.coalesce(func.sum(CA.call_count), 0)).where(
        *base_filter, CA.period_start >= today_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    calls_today = (await db.execute(q)).scalar() or 0

    # Calls MTD
    q = select(func.coalesce(func.sum(CA.call_count), 0)).where(
        *base_filter, CA.period_start >= mtd_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    calls_mtd = (await db.execute(q)).scalar() or 0

    # Active apps (from app cache)
    q = select(func.count()).select_from(AppCache).where(AppCache.is_active == True)  # noqa: E712
    if team_id:
        q = q.where(AppCache.team_id == team_id)
    active_apps = (await db.execute(q)).scalar() or 0

    # Online agents (apps seen recently)
    stale_cutoff = now - timedelta(minutes=5)
    q = select(func.count()).select_from(AppCache).where(
        AppCache.is_active == True,  # noqa: E712
        AppCache.last_seen_at >= stale_cutoff,
    )
    if team_id:
        q = q.where(AppCache.team_id == team_id)
    online_agents = (await db.execute(q)).scalar() or 0

    # Active teams (teams with activity in 30d)
    q = select(func.count(func.distinct(CA.team_id))).where(
        *base_filter, CA.period_start >= d30_start
    )
    q = _apply_team_scope(q, identity, CA.team_id)
    active_teams = (await db.execute(q)).scalar() or 0

    # Delta percentage
    cost_delta_pct_7d = None
    if cost_prior_7d and cost_prior_7d > 0:
        cost_delta_pct_7d = round(
            float((cost_7d - cost_prior_7d) / cost_prior_7d * 100), 1
        )

    # Cost per 1K tokens
    tokens_30d_q = select(func.coalesce(func.sum(CA.total_tokens), 0)).where(
        *base_filter, CA.period_start >= d30_start
    )
    tokens_30d_q = _apply_team_scope(tokens_30d_q, identity, CA.team_id)
    tokens_30d = (await db.execute(tokens_30d_q)).scalar() or 0

    cost_per_1k = None
    if tokens_30d > 0:
        cost_per_1k = round(float(cost_30d / tokens_30d * 1000), 4)

    # Data completeness
    completeness_pct, data_status = await _get_completeness(db)

    result = KpiSummary(
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
        cost_delta_pct_7d=cost_delta_pct_7d,
        cost_per_1k_tokens=cost_per_1k,
        data_completeness_pct=completeness_pct,
        data_status=data_status,
    )

    await cache.set(cache_key, result, CacheTier.HOT, completeness_pct or 100.0)
    return result


@router.get("/cost-over-time", response_model=list[TimeSeriesPoint], tags=["dashboard"])
async def cost_over_time(
    days: int = Query(7, ge=1, le=90),
    granularity: str = Query("daily"),
    team_id: Optional[str] = Query(None),
    app_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Cost over time chart data."""
    cache_key = f"cost-over-time:{days}:{granularity}:{team_id}:{app_id}:{provider}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    since = _window(days)
    CA = ConductorAggregate

    filters = [CA.granularity == granularity, CA.period_start >= since]
    if team_id:
        identity.assert_team_access(team_id)
        filters.append(CA.team_id == team_id)
    if app_id:
        filters.append(CA.app_id == app_id)
    if provider:
        filters.append(CA.provider == provider)

    # Group by date
    q = select(
        func.date(CA.period_start).label("day"),
        func.coalesce(func.sum(CA.total_cost), 0).label("cost"),
        func.coalesce(func.sum(CA.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(CA.call_count), 0).label("calls"),
    ).where(*filters).group_by(
        func.date(CA.period_start)
    ).order_by(
        func.date(CA.period_start)
    )
    q = _apply_team_scope(q, identity, CA.team_id)

    result = await db.execute(q)
    rows = result.all()

    points = [
        TimeSeriesPoint(
            period=datetime.combine(r.day, time.min, tzinfo=timezone.utc)
            if isinstance(r.day, date) and not isinstance(r.day, datetime)
            else (
                datetime.strptime(str(r.day), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if isinstance(r.day, str)
                else r.day
            ),
            cost=r.cost,
            tokens=r.tokens,
            calls=r.calls,
        )
        for r in rows
    ]

    await cache.set(cache_key, points, CacheTier.WARM)
    return points


@router.get("/by-provider", response_model=list[ProviderBreakdown], tags=["dashboard"])
async def by_provider(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Cost breakdown by AI provider."""
    cache_key = f"by-provider:{days}:{team_id}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    since = _window(days)
    CA = ConductorAggregate

    filters = [CA.granularity == "daily", CA.period_start >= since]
    if team_id:
        identity.assert_team_access(team_id)
        filters.append(CA.team_id == team_id)

    q = select(
        CA.provider,
        func.coalesce(func.sum(CA.total_cost), 0).label("cost"),
        func.coalesce(func.sum(CA.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(CA.call_count), 0).label("calls"),
    ).where(*filters).group_by(CA.provider).order_by(
        func.sum(CA.total_cost).desc()
    )
    q = _apply_team_scope(q, identity, CA.team_id)

    result = await db.execute(q)
    rows = result.all()

    total_cost = sum(r.cost for r in rows) or Decimal("1")
    providers = [
        ProviderBreakdown(
            provider=r.provider,
            cost=r.cost,
            tokens=r.tokens,
            calls=r.calls,
            cost_pct=round(float(r.cost / total_cost * 100), 1),
        )
        for r in rows
    ]

    await cache.set(cache_key, providers, CacheTier.WARM)
    return providers


@router.get("/by-app", response_model=list[AppBreakdown], tags=["dashboard"])
async def by_app(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Cost breakdown by application."""
    cache_key = f"by-app:{days}:{team_id}:{limit}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    since = _window(days)
    CA = ConductorAggregate

    filters = [CA.granularity == "daily", CA.period_start >= since]
    if team_id:
        identity.assert_team_access(team_id)
        filters.append(CA.team_id == team_id)

    q = select(
        CA.app_id,
        func.max(CA.app_name).label("app_name"),
        func.max(CA.team_slug).label("team_slug"),
        func.max(CA.environment).label("environment"),
        func.coalesce(func.sum(CA.total_cost), 0).label("cost"),
        func.coalesce(func.sum(CA.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(CA.call_count), 0).label("calls"),
    ).where(*filters).group_by(CA.app_id).order_by(
        func.sum(CA.total_cost).desc()
    ).limit(limit)
    q = _apply_team_scope(q, identity, CA.team_id)

    result = await db.execute(q)
    rows = result.all()

    apps = [
        AppBreakdown(
            app_id=r.app_id,
            app_name=r.app_name or r.app_id,
            team_slug=r.team_slug or "",
            environment=r.environment or "production",
            cost=r.cost,
            tokens=r.tokens,
            calls=r.calls,
        )
        for r in rows
    ]

    await cache.set(cache_key, apps, CacheTier.WARM)
    return apps


@router.get("/by-team", response_model=list[TeamBreakdown], tags=["dashboard"])
async def by_team(
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Cost breakdown by team — org-wide view."""
    cache_key = f"by-team:{days}:{limit}:{offset}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    since = _window(days)
    CA = ConductorAggregate

    q = select(
        CA.team_id,
        func.max(CA.team_slug).label("team_slug"),
        func.max(CA.team_name).label("team_name"),
        func.coalesce(func.sum(CA.total_cost), 0).label("cost"),
        func.coalesce(func.sum(CA.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(CA.call_count), 0).label("calls"),
        func.count(func.distinct(CA.app_id)).label("app_count"),
    ).where(
        CA.granularity == "daily",
        CA.period_start >= since,
    ).group_by(CA.team_id).order_by(
        func.sum(CA.total_cost).desc()
    ).limit(limit).offset(offset)
    q = _apply_team_scope(q, identity, CA.team_id)

    result = await db.execute(q)
    rows = result.all()

    teams = [
        TeamBreakdown(
            team_slug=r.team_slug or "",
            team_name=r.team_name or r.team_slug or "",
            cost=r.cost,
            tokens=r.tokens,
            calls=r.calls,
            app_count=r.app_count,
        )
        for r in rows
    ]

    await cache.set(cache_key, teams, CacheTier.WARM)
    return teams


@router.get("/top-models", response_model=list[ModelBreakdown], tags=["dashboard"])
async def top_models(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Most expensive models."""
    cache_key = f"top-models:{days}:{team_id}:{limit}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    since = _window(days)
    CA = ConductorAggregate

    filters = [
        CA.granularity == "daily",
        CA.period_start >= since,
        CA.model.isnot(None),
    ]
    if team_id:
        identity.assert_team_access(team_id)
        filters.append(CA.team_id == team_id)

    q = select(
        CA.provider,
        CA.model,
        func.coalesce(func.sum(CA.total_cost), 0).label("cost"),
        func.coalesce(func.sum(CA.call_count), 0).label("calls"),
        func.coalesce(func.sum(CA.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(CA.output_tokens), 0).label("output_tokens"),
    ).where(*filters).group_by(CA.provider, CA.model).order_by(
        func.sum(CA.total_cost).desc()
    ).limit(limit)
    q = _apply_team_scope(q, identity, CA.team_id)

    result = await db.execute(q)
    rows = result.all()

    models = [
        ModelBreakdown(
            provider=r.provider,
            model=r.model,
            cost=r.cost,
            calls=r.calls,
            avg_input_tokens=int(r.input_tokens / r.calls) if r.calls else None,
            avg_output_tokens=int(r.output_tokens / r.calls) if r.calls else None,
        )
        for r in rows
    ]

    await cache.set(cache_key, models, CacheTier.WARM)
    return models


@router.get("/recent-alerts", response_model=list[AlertSummary], tags=["dashboard"])
async def recent_alerts(
    limit: int = Query(20, ge=1, le=100),
    unacknowledged_only: bool = Query(False),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Recent alerts from all Orchestrators."""
    since = _window(7)

    filters = [AlertCache.fired_at >= since]
    if unacknowledged_only:
        filters.append(AlertCache.acknowledged == False)  # noqa: E712

    q = select(AlertCache).where(*filters).order_by(
        AlertCache.fired_at.desc()
    ).limit(limit)
    q = _apply_team_scope(q, identity, AlertCache.team_id)

    result = await db.execute(q)
    alerts = result.scalars().all()

    return [
        AlertSummary(
            id=a.id,
            severity=a.severity,
            metric=a.metric,
            threshold_value=a.threshold_value,
            actual_value=a.actual_value,
            app_id=a.app_id,
            team_id=a.team_id,
            fired_at=a.fired_at,
            acknowledged=a.acknowledged,
        )
        for a in alerts
    ]


@router.get("/app-status", response_model=list[AppStatus], tags=["dashboard"])
async def app_status(
    team_id: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Agent online/offline status across all Orchestrators."""
    cache_key = f"app-status:{team_id or 'all'}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    stale_cutoff = _now() - timedelta(minutes=5)

    filters = [AppCache.is_active == True]  # noqa: E712
    if team_id:
        identity.assert_team_access(team_id)
        filters.append(AppCache.team_id == team_id)

    q = select(AppCache).where(*filters).order_by(
        func.coalesce(AppCache.last_seen_at, datetime(1970, 1, 1, tzinfo=timezone.utc)).desc()
    )
    q = _apply_team_scope(q, identity, AppCache.team_id)

    result = await db.execute(q)
    apps = result.scalars().all()

    # Get team slugs from cache
    team_ids = list({a.team_id for a in apps})
    team_slugs = {}
    if team_ids:
        team_result = await db.execute(
            select(TeamCache.id, TeamCache.slug).where(TeamCache.id.in_(team_ids))
        )
        team_slugs = {r.id: r.slug for r in team_result.all()}

    statuses = [
        AppStatus(
            app_uuid=a.id,
            app_id=a.app_id,
            app_name=a.app_name,
            team_slug=team_slugs.get(a.team_id, ""),
            environment=a.environment,
            online=a.last_seen_at is not None and a.last_seen_at >= stale_cutoff,
            last_seen_at=a.last_seen_at,
            agent_version=a.agent_version,
            instrumented_providers=a.instrumented_providers,
        )
        for a in apps
    ]

    await cache.set(cache_key, statuses, CacheTier.HOT)
    return statuses


@router.get("/executive-charts", response_model=ExecutiveCharts, tags=["dashboard"])
async def executive_charts(
    days: int = Query(30, ge=1, le=90),
    team_id: Optional[str] = Query(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Executive dashboard charts — savings, enforcement mix, provider allocation."""
    cache_key = f"executive-charts:{days}:{team_id}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached

    since = _window(days)

    # 1. Savings over time (from denied/throttled policy decisions)
    savings_filters = [
        PolicyDecisionCache.decided_at >= since,
        PolicyDecisionCache.decision.in_(["deny", "throttle"]),
    ]
    if team_id:
        identity.assert_team_access(team_id)
        savings_filters.append(PolicyDecisionCache.team_id == team_id)

    savings_q = select(
        func.date(PolicyDecisionCache.decided_at).label("day"),
        func.coalesce(func.sum(PolicyDecisionCache.estimated_cost), 0).label("cost"),
    ).where(*savings_filters).group_by(
        func.date(PolicyDecisionCache.decided_at)
    ).order_by(func.date(PolicyDecisionCache.decided_at))

    savings_result = await db.execute(savings_q)
    savings_rows = savings_result.all()

    savings_points = [
        TimeSeriesPoint(
            period=datetime.combine(r.day, time.min, tzinfo=timezone.utc)
            if isinstance(r.day, date) and not isinstance(r.day, datetime)
            else (
                datetime.strptime(str(r.day), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if isinstance(r.day, str)
                else r.day
            ),
            cost=r.cost,
            tokens=0,
            calls=0,
        )
        for r in savings_rows
    ]

    # 2. Enforcement mix
    mix_filters = [PolicyDecisionCache.decided_at >= since]
    if team_id:
        mix_filters.append(PolicyDecisionCache.team_id == team_id)

    mix_q = select(
        PolicyDecisionCache.decision,
        func.count().label("count"),
    ).where(*mix_filters).group_by(PolicyDecisionCache.decision)

    mix_result = await db.execute(mix_q)
    enforcement_mix = {r.decision: r.count for r in mix_result.all()}

    # 3. Provider allocation (reuse by_provider logic)
    CA = ConductorAggregate
    prov_filters = [CA.granularity == "daily", CA.period_start >= since]
    if team_id:
        prov_filters.append(CA.team_id == team_id)

    prov_q = select(
        CA.provider,
        func.coalesce(func.sum(CA.total_cost), 0).label("cost"),
        func.coalesce(func.sum(CA.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(CA.call_count), 0).label("calls"),
    ).where(*prov_filters).group_by(CA.provider).order_by(
        func.sum(CA.total_cost).desc()
    )
    prov_q = _apply_team_scope(prov_q, identity, CA.team_id)

    prov_result = await db.execute(prov_q)
    prov_rows = prov_result.all()

    total_cost = sum(r.cost for r in prov_rows) or Decimal("1")
    provider_allocation = [
        ProviderBreakdown(
            provider=r.provider,
            cost=r.cost,
            tokens=r.tokens,
            calls=r.calls,
            cost_pct=round(float(r.cost / total_cost * 100), 1),
        )
        for r in prov_rows
    ]

    charts = ExecutiveCharts(
        savings_over_time=savings_points,
        enforcement_mix=enforcement_mix,
        provider_allocation=provider_allocation,
    )

    await cache.set(cache_key, charts, CacheTier.WARM)
    return charts
