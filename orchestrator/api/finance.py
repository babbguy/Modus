"""
Modus — Finance Intelligence API
==========================================
Endpoints for finance leads, VPs of Engineering,
and cost-center owners.

GET  /api/v1/finance/burn-rate          — budget burn rate per team/cost-center
GET  /api/v1/finance/chargeback         — enhanced chargeback with cost-center mapping
POST /api/v1/finance/chargeback/generate — generate chargeback invoice record
GET  /api/v1/finance/allocation         — cost allocation matrix
POST /api/v1/finance/allocation         — create/update cost-center mappings
GET  /api/v1/finance/scenarios          — list saved scenarios
POST /api/v1/finance/scenarios          — run a what-if scenario
GET  /api/v1/finance/reconciliation     — billing reconciliation (inferred vs actual)
POST /api/v1/finance/reconciliation/import     — import provider invoice totals (JSON)
POST /api/v1/finance/reconciliation/import/csv — import provider invoice totals (CSV upload)
GET  /api/v1/finance/audit-export       — audit trail export
GET  /api/v1/finance/cost-centers       — list cost centers
POST /api/v1/finance/cost-centers       — create cost center
GET  /api/v1/finance/summary            — finance dashboard summary (all widgets)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import (
    Team, AuditLog, CostCenter, TeamCostCenter, ChargebackInvoice, FinanceReport,
    BillingActual, ScenarioConfig, BillingConnection,
    ForecastConfig,
)
from orchestrator.db.session import get_session, sqlite_dt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/finance", tags=["finance"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class BurnRateEntry(BaseModel):
    team_slug: str
    team_name: str
    cost_center_code: Optional[str] = None
    department: Optional[str] = None
    budget_monthly_usd: Optional[str] = None
    current_spend_usd: str
    projected_eom_usd: str
    burn_pct: str
    risk: str  # on-track, at-risk, over-budget
    days_remaining: int


class BurnRateResponse(BaseModel):
    period: str
    teams: list[BurnRateEntry]
    total_budget_usd: str
    total_spend_usd: str
    total_projected_eom_usd: str
    overall_risk: str


class CostCenterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    code: str = Field(..., min_length=1, max_length=64)
    department: Optional[str] = Field(None, max_length=256)
    division: Optional[str] = Field(None, max_length=256)
    budget_owner_name: Optional[str] = Field(None, max_length=256)
    budget_owner_email: Optional[str] = Field(None, max_length=320)
    parent_id: Optional[str] = None
    budget_monthly_usd: Optional[str] = None
    budget_quarterly_usd: Optional[str] = None


class CostCenterResponse(BaseModel):
    id: str
    name: str
    code: str
    department: Optional[str]
    division: Optional[str]
    budget_owner_name: Optional[str]
    budget_owner_email: Optional[str]
    parent_id: Optional[str]
    budget_monthly_usd: Optional[str]
    budget_quarterly_usd: Optional[str]
    is_active: bool
    team_count: int = 0
    current_spend_usd: str = "0.00"


class AllocationRequest(BaseModel):
    team_id: str
    cost_center_id: str
    allocation_pct: float = Field(100.0, ge=0, le=100)


class ScenarioRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    scenario_type: str = Field(..., pattern=r"^(model_swap|team_add|usage_scale|budget_change)$")
    parameters: dict


class ScenarioResponse(BaseModel):
    id: str
    name: str
    scenario_type: str
    parameters: dict
    result: Optional[dict]
    created_at: datetime


class ChargebackGenerateRequest(BaseModel):
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    cost_center_id: Optional[str] = None
    format: str = Field("json", pattern=r"^(json|csv)$")


class AuditExportEntry(BaseModel):
    timestamp: datetime
    actor_id: str
    action: str
    resource_type: str
    resource_id: Optional[str]
    team_id: Optional[str]
    details: Optional[dict]


class FinanceSummaryResponse(BaseModel):
    total_monthly_budget_usd: str
    total_current_spend_usd: str
    total_projected_eom_usd: str
    overall_burn_pct: str
    overall_risk: str
    cost_center_count: int
    teams_at_risk: int
    teams_over_budget: int
    top_spenders: list[dict]
    spend_by_department: list[dict]
    recent_invoices: list[dict]


class BillingConnectionRequest(BaseModel):
    provider_type: str  # aws, gcp, azure, oracle, snowflake, databricks, openai, anthropic, custom
    provider_name: str
    service_type: Optional[str] = None
    api_endpoint: Optional[str] = None
    auth_type: str = "api_key"
    credentials: Optional[dict] = None  # Will be encrypted before storage
    config: Optional[dict] = None
    sync_schedule: Optional[str] = None


class BillingConnectionResponse(BaseModel):
    id: str
    provider_type: str
    provider_name: str
    service_type: Optional[str]
    api_endpoint: Optional[str]
    auth_type: str
    status: str
    last_sync_at: Optional[datetime]
    last_error: Optional[str]
    sync_schedule: Optional[str]
    config: Optional[dict]
    has_credentials: bool  # True if credentials_encrypted is not null -- NEVER return actual credentials
    created_at: datetime


class SpendTrendResponse(BaseModel):
    days: list[str]
    series: list[dict]
    budget_line: Optional[str]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _classify_risk(burn_pct: Decimal) -> str:
    if burn_pct >= 100:
        return "over-budget"
    if burn_pct >= 80:
        return "at-risk"
    return "on-track"


def _project_eom(current_spend: Decimal, days_elapsed: int, days_in_month: int) -> Decimal:
    if days_elapsed <= 0:
        return current_spend
    daily_rate = current_spend / Decimal(days_elapsed)
    return (daily_rate * Decimal(days_in_month)).quantize(Decimal("0.01"))


# ── GET /finance/summary ──────────────────────────────────────────────────────

@router.get("/summary", response_model=FinanceSummaryResponse)
async def finance_summary(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Finance dashboard summary — all key metrics in one call."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_elapsed = max((now - month_start).days, 1)
    # Actual days in current month (handles Feb, 30/31-day months, leap years)
    next_month = month_start.replace(month=month_start.month % 12 + 1, day=1) if month_start.month < 12 else month_start.replace(year=month_start.year + 1, month=1, day=1)
    days_in_month = (next_month - month_start).days

    # Get all teams with their cost-center mappings (scoped to identity)
    q = (
        select(Team, CostCenter, TeamCostCenter)
        .outerjoin(TeamCostCenter, TeamCostCenter.team_id == Team.id)
        .outerjoin(CostCenter, CostCenter.id == TeamCostCenter.cost_center_id)
        .where(Team.deleted_at.is_(None))
    )
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(Team.id.in_(identity.team_ids))
    team_rows = (await db.execute(q)).all()

    # Get spend per team for current month.
    # No granularity filter — matches the overview endpoint's behaviour so
    # that MTD numbers are always consistent across views.
    spend_q = text("""
        SELECT team_id, SUM(total_cost) AS cost
        FROM usage_aggregates
        WHERE granularity = 'daily' AND period_start >= :start AND period_start < :end
        GROUP BY team_id
    """)
    spend_result = await db.execute(spend_q, {"start": sqlite_dt(month_start), "end": sqlite_dt(now)})
    _ZERO = Decimal("0")
    spend_by_team = {str(r.team_id): Decimal(str(r.cost)) if r.cost else _ZERO for r in spend_result.all()}

    total_budget = _ZERO
    total_spend = _ZERO
    total_projected = _ZERO
    at_risk = 0
    over_budget = 0
    dept_spend: dict[str, Decimal] = {}
    top_list: list[dict] = []

    for team, cc, tcc in team_rows:
        team_spend = spend_by_team.get(str(team.id), _ZERO)
        budget = (
            Decimal(str(team.budget_monthly_usd)) if team.budget_monthly_usd else
            Decimal(str(team.max_budget_usd or 0)) if team.max_budget_usd else
            Decimal(str(cc.budget_monthly_usd or 0)) if cc and cc.budget_monthly_usd else _ZERO
        )
        projected = _project_eom(team_spend, days_elapsed, days_in_month)
        burn = (team_spend / budget * 100) if budget > 0 else _ZERO
        risk = _classify_risk(burn)

        total_budget += budget
        total_spend += team_spend
        total_projected += projected
        if risk == "at-risk":
            at_risk += 1
        elif risk == "over-budget":
            over_budget += 1

        dept = cc.department if cc else "Unassigned"
        dept_spend[dept] = dept_spend.get(dept, _ZERO) + team_spend

        top_list.append({
            "team_slug": team.slug,
            "team_name": team.name,
            "spend_usd": str(team_spend.quantize(Decimal("0.01"))),
            "budget_usd": str(budget.quantize(Decimal("0.01"))),
            "burn_pct": str(burn.quantize(Decimal("0.1"))),
            "risk": risk,
        })

    top_list.sort(key=lambda x: Decimal(x["spend_usd"]), reverse=True)
    overall_burn = (total_spend / total_budget * 100) if total_budget > 0 else _ZERO

    # Recent invoices
    inv_result = await db.execute(
        select(ChargebackInvoice)
        .order_by(ChargebackInvoice.created_at.desc())
        .limit(5)
    )
    recent_invoices = [
        {
            "id": str(inv.id),
            "period_start": inv.period_start.isoformat(),
            "period_end": inv.period_end.isoformat(),
            "total_cost_usd": str(inv.total_cost_usd),
            "status": inv.status,
            "format": inv.format,
        }
        for inv in inv_result.scalars().all()
    ]

    return FinanceSummaryResponse(
        total_monthly_budget_usd=str(total_budget.quantize(Decimal("0.01"))),
        total_current_spend_usd=str(total_spend.quantize(Decimal("0.01"))),
        total_projected_eom_usd=str(total_projected.quantize(Decimal("0.01"))),
        overall_burn_pct=str(overall_burn.quantize(Decimal("0.1"))),
        overall_risk=_classify_risk(overall_burn),
        cost_center_count=(await db.execute(
            select(func.count(CostCenter.id)).where(CostCenter.is_active == True)
        )).scalar() or 0,
        teams_at_risk=at_risk,
        teams_over_budget=over_budget,
        top_spenders=top_list[:10],
        spend_by_department=[
            {"department": dept, "spend_usd": str(spend.quantize(Decimal("0.01")))}
            for dept, spend in sorted(dept_spend.items(), key=lambda x: x[1], reverse=True)
        ],
        recent_invoices=recent_invoices,
    )


# ── GET /finance/burn-rate ────────────────────────────────────────────────────

@router.get("/burn-rate", response_model=BurnRateResponse)
async def get_burn_rate(
    cost_center_id: Optional[str] = None,
    period: str = "current",  # "current" | "prior_month"
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Budget burn rate per team with EOM projection and risk classification.

    period:
      - "current" (default): month-to-date for the current calendar month
      - "prior_month": full prior calendar month (for week-over-week / period
        comparison toggles in the dashboard)
    """
    real_now = datetime.now(timezone.utc)
    real_month_start = real_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if period == "prior_month":
        # Anchor the window to the prior calendar month
        if real_month_start.month == 1:
            month_start = real_month_start.replace(year=real_month_start.year - 1, month=12)
        else:
            month_start = real_month_start.replace(month=real_month_start.month - 1)
        # End of prior month (= start of current month)
        now = real_month_start
    else:
        month_start = real_month_start
        now = real_now

    days_elapsed = max((now - month_start).days, 1)
    next_month = month_start.replace(month=month_start.month % 12 + 1, day=1) if month_start.month < 12 else month_start.replace(year=month_start.year + 1, month=1, day=1)
    days_in_month = (next_month - month_start).days

    # Teams with optional cost-center join (scoped to identity)
    q = (
        select(Team, CostCenter, TeamCostCenter)
        .outerjoin(TeamCostCenter, TeamCostCenter.team_id == Team.id)
        .outerjoin(CostCenter, CostCenter.id == TeamCostCenter.cost_center_id)
        .where(Team.deleted_at.is_(None))
    )
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(Team.id.in_(identity.team_ids))
    if cost_center_id:
        q = q.where(TeamCostCenter.cost_center_id == cost_center_id)
    team_rows = (await db.execute(q)).all()

    # Spend per team — no granularity filter, matches overview endpoint
    spend_q = text("""
        SELECT team_id, SUM(total_cost) AS cost
        FROM usage_aggregates
        WHERE granularity = 'daily' AND period_start >= :start AND period_start < :end
        GROUP BY team_id
    """)
    spend_result = await db.execute(spend_q, {"start": sqlite_dt(month_start), "end": sqlite_dt(now)})
    _ZERO = Decimal("0")
    spend_by_team = {str(r.team_id): Decimal(str(r.cost)) if r.cost else _ZERO for r in spend_result.all()}

    entries: list[BurnRateEntry] = []
    total_budget = _ZERO
    total_spend = _ZERO
    total_projected = _ZERO

    for team, cc, tcc in team_rows:
        spend = spend_by_team.get(str(team.id), _ZERO)
        budget = (
            Decimal(str(team.budget_monthly_usd)) if team.budget_monthly_usd else
            Decimal(str(team.max_budget_usd or 0)) if team.max_budget_usd else
            Decimal(str(cc.budget_monthly_usd or 0)) if cc and cc.budget_monthly_usd else _ZERO
        )
        projected = _project_eom(spend, days_elapsed, days_in_month)
        burn = (spend / budget * 100) if budget > 0 else _ZERO

        total_budget += budget
        total_spend += spend
        total_projected += projected

        entries.append(BurnRateEntry(
            team_slug=team.slug,
            team_name=team.name,
            cost_center_code=cc.code if cc else None,
            department=cc.department if cc else None,
            budget_monthly_usd=str(budget.quantize(Decimal("0.01"))) if budget > 0 else None,
            current_spend_usd=str(spend.quantize(Decimal("0.01"))),
            projected_eom_usd=str(projected.quantize(Decimal("0.01"))),
            burn_pct=str(burn.quantize(Decimal("0.1"))),
            risk=_classify_risk(burn),
            days_remaining=days_in_month - days_elapsed,
        ))

    entries.sort(key=lambda x: Decimal(x.burn_pct), reverse=True)
    overall_burn = (total_spend / total_budget * 100) if total_budget > 0 else _ZERO

    return BurnRateResponse(
        period=month_start.strftime("%Y-%m"),
        teams=entries,
        total_budget_usd=str(total_budget.quantize(Decimal("0.01"))),
        total_spend_usd=str(total_spend.quantize(Decimal("0.01"))),
        total_projected_eom_usd=str(total_projected.quantize(Decimal("0.01"))),
        overall_risk=_classify_risk(overall_burn),
    )


# ── Cost Centers CRUD ─────────────────────────────────────────────────────────

@router.get("/cost-centers", response_model=list[CostCenterResponse])
async def list_cost_centers(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """List all cost centers with team counts and current spend."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    result = await db.execute(
        select(CostCenter).where(CostCenter.is_active == True)
        .order_by(CostCenter.department, CostCenter.name)
    )
    centers = result.scalars().all()

    # Team counts per cost center
    tc_result = await db.execute(
        select(TeamCostCenter.cost_center_id, func.count(TeamCostCenter.id))
        .group_by(TeamCostCenter.cost_center_id)
    )
    team_counts = {str(r[0]): r[1] for r in tc_result.all()}

    # Spend per cost center (via team mapping) — no granularity filter
    spend_q = text("""
        SELECT tcc.cost_center_id, SUM(ua.total_cost) AS cost
        FROM usage_aggregates ua
        JOIN team_cost_centers tcc ON tcc.team_id = ua.team_id
        WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start AND ua.period_start < :end
        GROUP BY tcc.cost_center_id
    """)
    spend_result = await db.execute(spend_q, {"start": sqlite_dt(month_start), "end": sqlite_dt(now)})
    _ZERO = Decimal("0")
    spend_by_cc = {str(r.cost_center_id): Decimal(str(r.cost)) if r.cost else _ZERO for r in spend_result.all()}

    return [
        CostCenterResponse(
            id=str(cc.id),
            name=cc.name,
            code=cc.code,
            department=cc.department,
            division=cc.division,
            budget_owner_name=cc.budget_owner_name,
            budget_owner_email=cc.budget_owner_email,
            parent_id=cc.parent_id,
            budget_monthly_usd=str(Decimal(str(cc.budget_monthly_usd)).quantize(Decimal("0.01"))) if cc.budget_monthly_usd else None,
            budget_quarterly_usd=str(Decimal(str(cc.budget_quarterly_usd)).quantize(Decimal("0.01"))) if cc.budget_quarterly_usd else None,
            is_active=cc.is_active,
            team_count=team_counts.get(str(cc.id), 0),
            current_spend_usd=str(spend_by_cc.get(str(cc.id), _ZERO).quantize(Decimal("0.01"))),
        )
        for cc in centers
    ]


@router.post("/cost-centers", response_model=CostCenterResponse, status_code=201)
async def create_cost_center(
    body: CostCenterRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Create a new cost center."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    # Check for duplicate code
    existing = await db.execute(
        select(CostCenter).where(CostCenter.code == body.code)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Cost center code '{body.code}' already exists")

    cc = CostCenter(
        name=body.name,
        code=body.code,
        department=body.department,
        division=body.division,
        budget_owner_name=body.budget_owner_name,
        budget_owner_email=body.budget_owner_email,
        parent_id=body.parent_id,
        budget_monthly_usd=Decimal(str(body.budget_monthly_usd)) if body.budget_monthly_usd else None,
        budget_quarterly_usd=Decimal(str(body.budget_quarterly_usd)) if body.budget_quarterly_usd else None,
    )
    db.add(cc)
    await db.flush()

    return CostCenterResponse(
        id=str(cc.id), name=cc.name, code=cc.code,
        department=cc.department, division=cc.division,
        budget_owner_name=cc.budget_owner_name,
        budget_owner_email=cc.budget_owner_email,
        parent_id=cc.parent_id,
        budget_monthly_usd=str(Decimal(str(cc.budget_monthly_usd)).quantize(Decimal("0.01"))) if cc.budget_monthly_usd else None,
        budget_quarterly_usd=str(Decimal(str(cc.budget_quarterly_usd)).quantize(Decimal("0.01"))) if cc.budget_quarterly_usd else None,
        is_active=cc.is_active,
        team_count=0,
        current_spend_usd="0.00",
    )


# ── Cost Allocation ───────────────────────────────────────────────────────────

@router.get("/allocation")
async def get_allocation(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Cost allocation matrix: teams mapped to cost centers."""
    result = await db.execute(
        select(TeamCostCenter, Team.slug, Team.name, CostCenter.code, CostCenter.department)
        .join(Team, Team.id == TeamCostCenter.team_id)
        .join(CostCenter, CostCenter.id == TeamCostCenter.cost_center_id)
        .order_by(CostCenter.department, Team.slug)
    )
    return [
        {
            "team_id": str(tcc.team_id),
            "team_slug": team_slug,
            "team_name": team_name,
            "cost_center_id": str(tcc.cost_center_id),
            "cost_center_code": cc_code,
            "department": dept,
            "allocation_pct": str(Decimal(str(tcc.allocation_pct)).quantize(Decimal("0.01"))),
        }
        for tcc, team_slug, team_name, cc_code, dept in result.all()
    ]


@router.post("/allocation", status_code=201)
async def set_allocation(
    body: AllocationRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Map a team to a cost center (upserts)."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    # Verify team and cost center exist
    team = await db.get(Team, body.team_id)
    if not team or team.deleted_at:
        raise HTTPException(status_code=404, detail="Team not found")
    cc = await db.get(CostCenter, body.cost_center_id)
    if not cc or not cc.is_active:
        raise HTTPException(status_code=404, detail="Cost center not found")

    # Upsert
    existing = await db.execute(
        select(TeamCostCenter).where(TeamCostCenter.team_id == body.team_id)
    )
    tcc = existing.scalar_one_or_none()
    if tcc:
        tcc.cost_center_id = body.cost_center_id
        tcc.allocation_pct = Decimal(str(body.allocation_pct))
    else:
        tcc = TeamCostCenter(
            team_id=body.team_id,
            cost_center_id=body.cost_center_id,
            allocation_pct=Decimal(str(body.allocation_pct)),
        )
        db.add(tcc)

    return {"status": "ok", "team_id": body.team_id, "cost_center_id": body.cost_center_id}


# ── Chargeback ────────────────────────────────────────────────────────────────

@router.get("/chargeback")
async def get_chargeback(
    period: Optional[str] = Query(None, description="Period: YYYY-MM, or omit for current month"),
    cost_center_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Enhanced chargeback with cost-center grouping."""
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
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_end = now

    q = text("""
        SELECT
            t.slug               AS team_slug,
            t.name               AS team_name,
            COALESCE(cc.code, 'UNASSIGNED') AS cost_center_code,
            COALESCE(cc.department, 'Unassigned') AS department,
            a.app_name,
            ua.provider,
            ua.model,
            SUM(ua.call_count)   AS calls,
            SUM(ua.total_tokens) AS tokens,
            SUM(ua.total_cost)   AS cost
        FROM usage_aggregates ua
        JOIN apps    a ON a.id = ua.app_id
        JOIN teams   t ON t.id = ua.team_id
        LEFT JOIN team_cost_centers tcc ON tcc.team_id = t.id
        LEFT JOIN cost_centers cc ON cc.id = tcc.cost_center_id
        WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start
          AND ua.period_start < :end
          AND (CAST(:cc_id AS TEXT) IS NULL OR CAST(tcc.cost_center_id AS TEXT) = CAST(:cc_id AS TEXT))
        GROUP BY t.slug, t.name, cc.code, cc.department, a.app_name, ua.provider, ua.model
        ORDER BY SUM(ua.total_cost) DESC
    """)

    result = await db.execute(q, {
        "start": sqlite_dt(period_start),
        "end": sqlite_dt(period_end),
        "cc_id": cost_center_id,
    })

    return [
        {
            "team_slug": r.team_slug,
            "team_name": r.team_name,
            "cost_center_code": r.cost_center_code,
            "department": r.department,
            "app_name": r.app_name,
            "provider": r.provider,
            "model": r.model,
            "calls": int(r.calls or 0),
            "tokens": int(r.tokens or 0),
            "cost": str(r.cost or 0),
        }
        for r in result.all()
    ]


@router.post("/chargeback/generate", status_code=201)
async def generate_chargeback_invoice(
    body: ChargebackGenerateRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Generate a chargeback invoice record."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    now = datetime.now(timezone.utc)
    period_start = body.period_start or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_end = body.period_end or now

    # Get the chargeback data
    q = text("""
        SELECT
            t.slug AS team_slug, t.name AS team_name,
            a.app_name, ua.provider, ua.model,
            SUM(ua.call_count) AS calls,
            SUM(ua.total_tokens) AS tokens,
            SUM(ua.total_cost) AS cost
        FROM usage_aggregates ua
        JOIN apps a ON a.id = ua.app_id
        JOIN teams t ON t.id = ua.team_id
        LEFT JOIN team_cost_centers tcc ON tcc.team_id = t.id
        WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start AND ua.period_start < :end
          AND (CAST(:cc_id AS TEXT) IS NULL OR CAST(tcc.cost_center_id AS TEXT) = CAST(:cc_id AS TEXT))
        GROUP BY t.slug, t.name, a.app_name, ua.provider, ua.model
        ORDER BY SUM(ua.total_cost) DESC
    """)
    result = await db.execute(q, {
        "start": sqlite_dt(period_start), "end": sqlite_dt(period_end), "cc_id": body.cost_center_id,
    })
    rows = result.all()

    line_items = [
        {
            "team": r.team_slug,
            "team_name": r.team_name,
            "app": r.app_name,
            "provider": r.provider,
            "model": r.model,
            "calls": int(r.calls or 0),
            "tokens": int(r.tokens or 0),
            "cost": str(r.cost or 0),
        }
        for r in rows
    ]
    total = sum((Decimal(item["cost"]) for item in line_items), Decimal("0"))

    invoice = ChargebackInvoice(
        cost_center_id=body.cost_center_id,
        period_start=period_start,
        period_end=period_end,
        total_cost_usd=total.quantize(Decimal("0.00000001")),
        line_items=line_items,
        format=body.format,
        status="generated",
        generated_by=identity.actor_id,
    )
    db.add(invoice)
    await db.flush()

    return {
        "id": str(invoice.id),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "total_cost_usd": str(total.quantize(Decimal("0.01"))),
        "line_item_count": len(line_items),
        "format": body.format,
        "line_items": line_items,
    }


# ── What-If Scenarios ─────────────────────────────────────────────────────────

@router.get("/scenarios", response_model=list[ScenarioResponse])
async def list_scenarios(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """List saved what-if scenarios."""
    result = await db.execute(
        select(ScenarioConfig).order_by(ScenarioConfig.created_at.desc()).limit(50)
    )
    return [
        ScenarioResponse(
            id=str(s.id), name=s.name, scenario_type=s.scenario_type,
            parameters=s.parameters, result=s.result, created_at=s.created_at,
        )
        for s in result.scalars().all()
    ]


@router.post("/scenarios", response_model=ScenarioResponse, status_code=201)
async def run_scenario(
    body: ScenarioRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Run a what-if scenario against actual historical data.

    Scenario types:
    - model_swap: {team_id?, from_model, to_model} — project savings if model is swapped
    - usage_scale: {team_id?, scale_factor} — project cost at N× current usage
    - team_add: {num_apps, avg_cost_per_app_monthly} — project cost of adding a team
    - budget_change: {team_id, new_budget_monthly_usd} — project new burn rate
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = month_start.replace(month=month_start.month % 12 + 1, day=1) if month_start.month < 12 else month_start.replace(year=month_start.year + 1, month=1, day=1)
    days_in_month = (next_month - month_start).days
    params = body.parameters
    result: dict = {}

    if body.scenario_type == "model_swap":
        from_model = params.get("from_model", "")
        to_model = params.get("to_model", "")
        team_id = params.get("team_id")

        # Get current spend on from_model
        q = text("""
            SELECT SUM(ua.total_cost) AS cost, SUM(ua.call_count) AS calls,
                   SUM(ua.total_tokens) AS tokens
            FROM usage_aggregates ua
            WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start AND ua.period_start < :end
              AND ua.model = :from_model
              AND (CAST(:team_id AS TEXT) IS NULL OR CAST(ua.team_id AS TEXT) = CAST(:team_id AS TEXT))
        """)
        r = (await db.execute(q, {
            "start": sqlite_dt(month_start), "end": sqlite_dt(now),
            "from_model": from_model, "team_id": team_id,
        })).one()

        current_cost = Decimal(str(r.cost or 0))

        # Estimate new cost based on model pricing ratios
        # Use a conservative 3:1 ratio for expensive->cheap model swaps
        ratio = Decimal(str(params.get("cost_ratio", 0.33)))
        projected_cost = current_cost * ratio
        savings = current_cost - projected_cost
        days_factor = Decimal(days_in_month) / Decimal(max((now - month_start).days, 1))

        result = {
            "from_model": from_model,
            "to_model": to_model,
            "current_monthly_cost_usd": str((current_cost * days_factor).quantize(Decimal("0.01"))),
            "projected_monthly_cost_usd": str((projected_cost * days_factor).quantize(Decimal("0.01"))),
            "projected_monthly_savings_usd": str((savings * days_factor).quantize(Decimal("0.01"))),
            "calls_affected": int(r.calls or 0),
            "tokens_affected": int(r.tokens or 0),
        }

    elif body.scenario_type == "usage_scale":
        scale = params.get("scale_factor", 2.0)
        team_id = params.get("team_id")

        q = text("""
            SELECT SUM(ua.total_cost) AS cost
            FROM usage_aggregates ua
            WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start AND ua.period_start < :end
              AND (CAST(:team_id AS TEXT) IS NULL OR CAST(ua.team_id AS TEXT) = CAST(:team_id AS TEXT))
        """)
        r = (await db.execute(q, {
            "start": sqlite_dt(month_start), "end": sqlite_dt(now), "team_id": team_id,
        })).one()

        current = Decimal(str(r.cost or 0))
        days = max((now - month_start).days, 1)
        monthly = current * Decimal(days_in_month) / Decimal(days)
        scale_d = Decimal(str(scale))
        result = {
            "current_monthly_usd": str(monthly.quantize(Decimal("0.01"))),
            "scale_factor": scale,
            "projected_monthly_usd": str((monthly * scale_d).quantize(Decimal("0.01"))),
            "delta_monthly_usd": str((monthly * (scale_d - 1)).quantize(Decimal("0.01"))),
        }

    elif body.scenario_type == "team_add":
        num_apps = params.get("num_apps", 5)
        avg_cost = Decimal(str(params.get("avg_cost_per_app_monthly", 100.0)))
        result = {
            "num_apps": num_apps,
            "avg_cost_per_app_monthly_usd": str(avg_cost.quantize(Decimal("0.01"))),
            "projected_monthly_increase_usd": str((num_apps * avg_cost).quantize(Decimal("0.01"))),
            "projected_annual_increase_usd": str((num_apps * avg_cost * 12).quantize(Decimal("0.01"))),
        }

    elif body.scenario_type == "budget_change":
        team_id = params.get("team_id")
        new_budget = params.get("new_budget_monthly_usd", 0)
        if not team_id:
            raise HTTPException(status_code=400, detail="team_id required for budget_change")

        q = text("""
            SELECT SUM(ua.total_cost) AS cost
            FROM usage_aggregates ua
            WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start AND ua.period_start < :end
              AND ua.team_id = :team_id
        """)
        r = (await db.execute(q, {
            "start": sqlite_dt(month_start), "end": sqlite_dt(now), "team_id": team_id,
        })).one()

        current = Decimal(str(r.cost or 0))
        days = max((now - month_start).days, 1)
        projected_eom = current * Decimal(days_in_month) / Decimal(days)
        new_budget_d = Decimal(str(new_budget))
        new_burn = (projected_eom / new_budget_d * 100) if new_budget_d > 0 else Decimal("0")

        result = {
            "team_id": team_id,
            "current_spend_usd": str(current.quantize(Decimal("0.01"))),
            "projected_eom_usd": str(projected_eom.quantize(Decimal("0.01"))),
            "new_budget_monthly_usd": str(new_budget_d.quantize(Decimal("0.01"))),
            "projected_burn_pct": str(new_burn.quantize(Decimal("0.1"))),
            "risk": _classify_risk(new_burn),
        }

    # Save scenario
    scenario = ScenarioConfig(
        name=body.name,
        scenario_type=body.scenario_type,
        parameters=body.parameters,
        result=result,
        created_by=identity.actor_id,
    )
    db.add(scenario)
    await db.flush()

    return ScenarioResponse(
        id=str(scenario.id), name=scenario.name,
        scenario_type=scenario.scenario_type,
        parameters=scenario.parameters, result=result,
        created_at=scenario.created_at,
    )


# ── Billing Reconciliation ────────────────────────────────────────────────────

async def _tracked_cost(db: AsyncSession, provider: str, start: datetime, end: datetime) -> Decimal:
    """What Modus tracked for ``provider`` in [start, end): the sum of daily
    usage aggregates (the same source as the other Finance views)."""
    from orchestrator.core.billing_import import usage_providers_for

    names = usage_providers_for(provider)
    placeholders = ", ".join(f":p{i}" for i in range(len(names)))
    params: dict = {f"p{i}": n for i, n in enumerate(names)}
    params["start"] = sqlite_dt(start)
    params["end"] = sqlite_dt(end)
    result = await db.execute(text(f"""
        SELECT COALESCE(SUM(total_cost), 0) AS cost
        FROM usage_aggregates
        WHERE granularity = 'daily'
          AND provider IN ({placeholders})
          AND period_start >= :start AND period_start < :end
    """), params)
    # SUM() may come back as a float on SQLite; round away binary noise.
    return Decimal(str(result.scalar_one() or 0)).quantize(Decimal("0.00000001"))


def _reconciliation_row(ba: BillingActual, inferred: Decimal) -> dict:
    from orchestrator.core.billing_import import money_str, variance

    delta, pct = variance(ba.actual_cost_usd, inferred)
    return {
        "id": str(ba.id),
        "provider": ba.provider,
        "service": ba.service or None,
        "period_start": ba.period_start.isoformat(),
        "period_end": ba.period_end.isoformat(),
        "actual_cost_usd": money_str(ba.actual_cost_usd),
        "inferred_cost_usd": money_str(inferred),
        "delta_usd": money_str(delta),
        "delta_pct": pct,
        "reconciled_at": ba.reconciled_at.isoformat() if ba.reconciled_at else None,
    }


@router.get("/reconciliation")
async def get_reconciliation(
    provider: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Billing reconciliation: what the provider billed vs what Modus tracked.

    Rows come from imported invoice totals (``POST /finance/reconciliation/import``).
    ``inferred_cost_usd`` and the delta are recomputed from current usage data
    on every call, so late-arriving usage is reflected; ``reconciled_at`` is
    when the invoice row was last imported. delta = actual - inferred, so a
    positive delta means Modus tracked less than the provider billed.
    """
    q = select(BillingActual).order_by(BillingActual.period_start.desc()).limit(limit)
    if provider:
        q = q.where(BillingActual.provider == provider.strip().lower())

    result = await db.execute(q)
    rows = []
    for ba in result.scalars().all():
        inferred = await _tracked_cost(db, ba.provider, ba.period_start, ba.period_end)
        rows.append(_reconciliation_row(ba, inferred))
    return rows


class BillingImportBody(BaseModel):
    rows: list[dict] = Field(..., min_length=1)
    source: Optional[str] = Field(None, max_length=64, description="Free-text label, e.g. openai-invoice-2026-09")


def _import_error(errors) -> HTTPException:
    return HTTPException(422, {
        "message": "Billing actuals rejected; nothing was imported.",
        "errors": [e.model_dump(exclude_none=True) for e in errors],
    })


async def _import_actuals(rows, source: Optional[str], identity: Identity, request: Request, db: AsyncSession) -> dict:
    """Upsert validated rows on (provider, service, period_start, period_end)."""
    from orchestrator.core.billing_import import variance

    now = datetime.now(timezone.utc)
    created = updated = unchanged = 0
    out_rows = []
    for row in rows:
        service = row.service or ""
        existing = (await db.execute(
            select(BillingActual).where(
                BillingActual.provider == row.provider,
                BillingActual.service == service,
                BillingActual.period_start == row.period_start,
                BillingActual.period_end == row.period_end,
            )
        )).scalars().first()

        raw = {"source": source, "currency": row.currency, "imported_by": identity.actor_id}
        inferred = await _tracked_cost(db, row.provider, row.period_start, row.period_end)
        delta, pct = variance(row.actual_cost_usd, inferred)

        if existing is None:
            ba = BillingActual(
                provider=row.provider, service=service,
                period_start=row.period_start, period_end=row.period_end,
                actual_cost_usd=row.actual_cost_usd,
                inferred_cost_usd=inferred, delta_usd=delta, delta_pct=pct,
                raw_data=raw, reconciled_at=now,
            )
            db.add(ba)
            created += 1
        else:
            ba = existing
            if ba.actual_cost_usd == row.actual_cost_usd:
                unchanged += 1
            else:
                updated += 1
            ba.actual_cost_usd = row.actual_cost_usd
            ba.inferred_cost_usd = inferred
            ba.delta_usd = delta
            ba.delta_pct = pct
            ba.raw_data = raw
            ba.reconciled_at = now
        await db.flush()
        out_rows.append(_reconciliation_row(ba, inferred))

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        resource_type="billing_actual",
        resource_id=None,
        action="imported",
        after={
            "source": source, "rows": len(rows), "created": created,
            "updated": updated, "unchanged": unchanged,
            "providers": sorted({r.provider for r in rows}),
        },
    ))
    return {
        "imported": len(rows), "created": created, "updated": updated, "unchanged": unchanged,
        "rows": out_rows,
    }


@router.post("/reconciliation/import", status_code=200)
async def import_billing_actuals(
    body: BillingImportBody,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Import provider invoice totals (JSON). Platform admin only.

    Each row: ``provider``, optional ``service``, ``period_start`` and
    ``period_end`` (half-open, UTC; ``YYYY-MM-DD`` means midnight), and
    ``actual_cost_usd`` as a decimal string. Rows are matched on
    (provider, service, period_start, period_end), so re-importing updates
    instead of duplicating. The whole request is rejected with HTTP 422 and a
    per-row error list if any row is invalid.
    """
    from orchestrator.core.billing_import import MAX_ROWS, validate_rows

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    if len(body.rows) > MAX_ROWS:
        raise HTTPException(422, f"At most {MAX_ROWS} rows per request.")
    rows, errors = validate_rows(
        (i, r if isinstance(r, dict) else {}) for i, r in enumerate(body.rows, start=1)
    )
    if errors:
        raise _import_error(errors)
    return await _import_actuals(rows, body.source, identity, request, db)


@router.post("/reconciliation/import/csv", status_code=200)
async def import_billing_actuals_csv(
    request: Request,
    file: UploadFile = File(...),
    source: Optional[str] = Form(None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Import provider invoice totals from a CSV upload. Platform admin only.

    Header (required): ``provider,period_start,period_end,actual_cost_usd``;
    optional ``service`` and ``currency`` (USD only). Same rules and
    all-or-nothing behaviour as the JSON import; error row numbers are file
    line numbers.
    """
    from orchestrator.core.billing_import import MAX_CSV_BYTES, parse_csv, validate_rows

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    content = await file.read(MAX_CSV_BYTES + 1)
    raw_rows, parse_errors = parse_csv(content)
    if parse_errors:
        raise _import_error(parse_errors)
    rows, errors = validate_rows(raw_rows)
    if errors:
        raise _import_error(errors)
    label = (source or file.filename or "csv-upload")[:64]
    return await _import_actuals(rows, label, identity, request, db)


# ── Audit Trail Export ────────────────────────────────────────────────────────

@router.get("/audit-export")
async def export_audit_trail(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    team_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Export audit trail for compliance (SOC 2, ISO 27001)."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    now = datetime.now(timezone.utc)
    q = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)

    if start:
        q = q.where(AuditLog.occurred_at >= start)
    if end:
        q = q.where(AuditLog.occurred_at <= end)
    if team_id:
        q = q.where(AuditLog.team_id == team_id)
    if action:
        q = q.where(AuditLog.action == action)

    result = await db.execute(q)
    entries = []
    for log in result.scalars().all():
        entries.append({
            "timestamp": log.occurred_at.isoformat(),
            "actor_id": log.actor_id,
            "actor_ip": log.actor_ip,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "team_id": log.team_id,
            "before": log.before,
            "after": log.after,
        })

    return {
        "export_time": now.isoformat(),
        "record_count": len(entries),
        "filters": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "team_id": team_id,
            "action": action,
        },
        "records": entries,
    }


# ── Billing Connections ──────────────────────────────────────────────────────

@router.get("/connections", response_model=list[BillingConnectionResponse])
async def list_connections(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """List all billing provider connections."""
    result = await db.execute(
        select(BillingConnection).order_by(BillingConnection.created_at.desc())
    )
    return [
        BillingConnectionResponse(
            id=str(c.id),
            provider_type=c.provider_type,
            provider_name=c.provider_name,
            service_type=c.service_type,
            api_endpoint=c.api_endpoint,
            auth_type=c.auth_type,
            status=c.status,
            last_sync_at=c.last_sync_at,
            last_error=c.last_error,
            sync_schedule=c.sync_schedule,
            config=c.config,
            has_credentials=c.credentials_encrypted is not None,
            created_at=c.created_at,
        )
        for c in result.scalars().all()
    ]


@router.post("/connections", response_model=BillingConnectionResponse, status_code=201)
async def create_connection(
    body: BillingConnectionRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Create a new billing provider connection (platform admin only)."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    # Validate provider_type
    valid_providers = {"aws", "gcp", "azure", "oracle", "snowflake", "databricks", "openai", "anthropic", "cohere", "custom"}
    if body.provider_type not in valid_providers:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider_type. Must be one of: {', '.join(sorted(valid_providers))}",
        )

    # Validate auth_type
    valid_auth_types = {"api_key", "oauth2", "bearer_token", "bearer", "basic", "iam_role", "service_account"}
    if body.auth_type not in valid_auth_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid auth_type. Must be one of: {', '.join(sorted(valid_auth_types))}",
        )

    # Validate service_type if provided
    if body.service_type:
        valid_service_types = {"ai", "ai_provider", "compute", "cloud_compute", "storage", "database", "data_platform", "saas", "other"}
        if body.service_type not in valid_service_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid service_type. Must be one of: {', '.join(sorted(valid_service_types))}",
            )

    # Serialize and encrypt credentials before storage. Fails closed: if no
    # MODUS_ENCRYPTION_KEY is configured, refuse to persist plaintext.
    credentials_encrypted = None
    if body.credentials:
        from orchestrator.core.credential_crypto import (
            CredentialEncryptionError,
            encrypt_credential,
        )
        try:
            credentials_encrypted = encrypt_credential(json.dumps(body.credentials))
        except CredentialEncryptionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    conn = BillingConnection(
        provider_type=body.provider_type,
        provider_name=body.provider_name,
        service_type=body.service_type,
        api_endpoint=body.api_endpoint,
        auth_type=body.auth_type,
        credentials_encrypted=credentials_encrypted,
        config=body.config,
        sync_schedule=body.sync_schedule,
        created_by=identity.actor_id,
    )
    db.add(conn)
    await db.flush()

    return BillingConnectionResponse(
        id=str(conn.id),
        provider_type=conn.provider_type,
        provider_name=conn.provider_name,
        service_type=conn.service_type,
        api_endpoint=conn.api_endpoint,
        auth_type=conn.auth_type,
        status=conn.status,
        last_sync_at=conn.last_sync_at,
        last_error=conn.last_error,
        sync_schedule=conn.sync_schedule,
        config=conn.config,
        has_credentials=conn.credentials_encrypted is not None,
        created_at=conn.created_at,
    )


@router.delete("/connections/{connection_id}", status_code=204, response_model=None)
async def delete_connection(
    connection_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Delete a billing provider connection (platform admin only)."""
    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    conn = await db.get(BillingConnection, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    await db.delete(conn)


@router.post("/connections/{connection_id}/test")
async def test_connection(
    connection_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Test a billing provider connection.

    Only checks that credentials are stored; it does not contact the provider.
    Modus does not sync invoices automatically -- import provider totals with
    POST /api/v1/finance/reconciliation/import.
    """
    conn = await db.get(BillingConnection, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    if conn.credentials_encrypted:
        return {
            "status": "ok",
            "message": "Credentials are stored. The provider was not contacted and invoices are "
                       "not synced automatically; import them via /finance/reconciliation/import.",
        }
    else:
        return {"status": "error", "message": "No credentials configured"}


# ── Spend Trend ──────────────────────────────────────────────────────────────

@router.get("/spend-trend", response_model=SpendTrendResponse)
async def get_spend_trend(
    days: int = Query(30, ge=1, le=365),
    period: Optional[str] = Query(None, pattern="^(mtd)$"),
    team_id: Optional[str] = None,
    provider: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Daily spend time series for charts."""
    now = datetime.now(timezone.utc)
    if period == "mtd":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=days)

    # Build query for daily spend grouped by date and provider
    params: dict = {"start": sqlite_dt(start), "end": sqlite_dt(now)}
    where_clauses = [
        "ua.granularity = 'daily'", "ua.period_start >= :start",
        "ua.period_start < :end",
    ]

    if team_id:
        where_clauses.append("ua.team_id = :team_id")
        params["team_id"] = team_id
    if provider:
        where_clauses.append("ua.provider = :provider")
        params["provider"] = provider

    where_sql = " AND ".join(where_clauses)

    q = text(f"""
        SELECT
            DATE(ua.period_start) AS day,
            ua.provider,
            SUM(ua.total_cost) AS cost
        FROM usage_aggregates ua
        WHERE {where_sql}
        GROUP BY DATE(ua.period_start), ua.provider
        ORDER BY DATE(ua.period_start)
    """)

    result = await db.execute(q, params)
    rows = result.all()

    # Build date range
    day_labels: list[str] = []
    current = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while current < now:
        day_labels.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    # Collect per-provider data
    _ZERO = Decimal("0")
    provider_data: dict[str, dict[str, Decimal]] = {}  # provider -> {day_str: cost}
    for row in rows:
        day_str = str(row.day)[:10]  # Ensure YYYY-MM-DD format
        prov = row.provider
        if prov not in provider_data:
            provider_data[prov] = {}
        provider_data[prov][day_str] = Decimal(str(row.cost)) if row.cost else _ZERO

    # Build series
    series: list[dict] = []

    # Total series
    total_data: list[str] = []
    for day in day_labels:
        day_total = sum(
            (pdata.get(day, _ZERO) for pdata in provider_data.values()), _ZERO
        )
        total_data.append(str(day_total.quantize(Decimal("0.01"))))
    series.append({"label": "Total", "data": total_data})

    # Per-provider series
    for prov in sorted(provider_data.keys()):
        prov_data = [str(provider_data[prov].get(day, _ZERO).quantize(Decimal("0.01"))) for day in day_labels]
        series.append({"label": prov, "data": prov_data})

    # Budget line: try to get the monthly budget for the team (or total across all teams)
    budget_line: Optional[str] = None
    if team_id:
        team = await db.get(Team, team_id)
        if team and team.budget_monthly_usd:
            budget_line = str(Decimal(str(team.budget_monthly_usd)).quantize(Decimal("0.01")))
    else:
        # Sum of all team budgets
        budget_result = await db.execute(
            select(func.sum(Team.budget_monthly_usd)).where(
                Team.deleted_at.is_(None),
                Team.budget_monthly_usd.isnot(None),
            )
        )
        total_budget = budget_result.scalar()
        if total_budget:
            budget_line = str(Decimal(str(total_budget)).quantize(Decimal("0.01")))

    return SpendTrendResponse(
        days=day_labels,
        series=series,
        budget_line=budget_line,
    )


# ── Three-Tier Forecasting ──────────────────────────────────────────────────


class ForecastRequest(BaseModel):
    team_id: Optional[str] = None
    method: str = Field("auto", pattern=r"^(auto|builtin|custom_ml|ai)$")
    basis_days: int = Field(28, ge=7, le=90)


class ForecastResponse(BaseModel):
    team_id: Optional[str]
    team_name: Optional[str]
    forecast_eom: str
    forecast_eoq: str
    forecast_eoy: str
    confidence_low_eom: str
    confidence_high_eom: str
    trend_daily: Optional[str] = None
    trend: Optional[str] = None
    trend_pct_monthly: Optional[str] = None
    method: str
    r_squared: float
    seasonal_factors: list[float]
    breach_prediction: Optional[dict] = None
    narrative: Optional[str] = None
    patterns_detected: Optional[list[str]] = None
    risk_level: Optional[str] = None
    mtd_actual: str
    budget_monthly: Optional[str] = None
    computed_at: datetime


class ForecastConfigRequest(BaseModel):
    team_id: Optional[str] = None
    method: str = Field("auto", pattern=r"^(auto|builtin|custom_ml|ai)$")
    custom_ml_url: Optional[str] = None
    custom_ml_auth: Optional[str] = None
    custom_ml_timeout_seconds: int = Field(30, ge=5, le=120)


class ForecastConfigResponse(BaseModel):
    id: str
    team_id: Optional[str]
    method: str
    custom_ml_url: Optional[str]
    has_ml_credentials: bool
    custom_ml_timeout_seconds: int
    is_active: bool
    created_at: datetime


class PriceImpactRequest(BaseModel):
    provider: str
    price_change_pct: float = Field(..., ge=-100, le=1000)
    days: int = Field(30, ge=7, le=365)


class BudgetBreachResponse(BaseModel):
    team_id: str
    team_name: str
    budget_monthly: str
    mtd_actual: str
    breach_predicted: bool
    breach_date: Optional[str] = None
    breach_day: Optional[int] = None
    projected_overage: Optional[str] = None
    confidence: Optional[str] = None
    already_breached: bool = False


@router.post("/forecast", response_model=ForecastResponse)
async def run_forecast(
    body: ForecastRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Three-tier spend forecast: built-in (OLS + Holt + seasonal), customer ML,
    or AI-powered via the customer's own configured provider.

    Methods:
    - auto: AI if configured, else built-in
    - builtin: Pure Python (OLS + double exponential + seasonal decomposition)
    - custom_ml: Customer's own ML endpoint (fallback to built-in)
    - ai: Customer's own AI provider (fallback to built-in)
    """
    from orchestrator.core.forecast_engine import get_forecast

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    basis_days = body.basis_days
    window_start = now - timedelta(days=basis_days)

    # Calendar math
    if now.month < 12:
        month_end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    else:
        month_end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    days_remaining_month = max(0, (month_end - now).days)

    q_end_month = ((now.month - 1) // 3 + 1) * 3
    if q_end_month > 12:
        q_end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        q_end = datetime(now.year, q_end_month + 1, 1, tzinfo=timezone.utc) if q_end_month < 12 else datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    days_remaining_quarter = max(0, (q_end - now).days)

    y_end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    days_remaining_year = max(0, (y_end - now).days)

    # Resolve team
    team_name = "Organization"
    team_budget = None
    if body.team_id:
        team = await db.get(Team, body.team_id)
        if not team or team.deleted_at:
            raise HTTPException(status_code=404, detail="Team not found")
        team_name = team.name
        team_budget = str(Decimal(str(team.budget_monthly_usd)).quantize(Decimal("0.01"))) if team.budget_monthly_usd else None

    # Fetch daily costs — all user-supplied values go through :param placeholders
    team_filter = " AND ua.team_id = :tid" if body.team_id else ""
    base_params: dict = {"start": sqlite_dt(window_start), "end": sqlite_dt(now)}
    if body.team_id:
        base_params["tid"] = body.team_id

    daily_q = text(
        "SELECT DATE(ua.period_start) AS day, SUM(ua.total_cost) AS cost"
        " FROM usage_aggregates ua"
        " WHERE ua.granularity = 'daily'"
        " AND ua.period_start >= :start AND ua.period_start < :end"
        + team_filter
        + " GROUP BY DATE(ua.period_start)"
        " ORDER BY DATE(ua.period_start)"
    )
    result = await db.execute(daily_q, base_params)
    rows = result.all()

    daily_costs = [float(r.cost or 0) for r in rows]  # forecast_engine expects float lists
    daily_labels = [str(r.day)[:10] for r in rows]

    # MTD actual
    mtd_params: dict = {"start": sqlite_dt(month_start), "end": sqlite_dt(now)}
    if body.team_id:
        mtd_params["tid"] = body.team_id

    mtd_result = await db.execute(
        text(
            "SELECT COALESCE(SUM(ua.total_cost), 0) AS mtd"
            " FROM usage_aggregates ua"
            " WHERE ua.granularity = 'daily'"
            " AND ua.period_start >= :start AND ua.period_start < :end"
            + team_filter
        ),
        mtd_params,
    )
    mtd_actual = float(mtd_result.scalar() or 0)

    # Provider breakdown (for AI/ML context)
    prov_params: dict = {"start": sqlite_dt(window_start), "end": sqlite_dt(now)}
    if body.team_id:
        prov_params["tid"] = body.team_id

    prov_result = await db.execute(
        text(
            "SELECT ua.provider, SUM(ua.total_cost) AS cost"
            " FROM usage_aggregates ua"
            " WHERE ua.granularity = 'daily'"
            " AND ua.period_start >= :start AND ua.period_start < :end"
            + team_filter
            + " GROUP BY ua.provider"
        ),
        prov_params,
    )
    provider_breakdown = {r.provider: float(r.cost or 0) for r in prov_result.all()}  # forecast_engine expects float

    # Resolve forecast config (custom ML settings)
    custom_ml_url = None
    custom_ml_auth = None
    method = body.method

    fc_result = await db.execute(
        select(ForecastConfig).where(
            ForecastConfig.team_id == body.team_id,
            ForecastConfig.is_active == True,
        )
    )
    fc = fc_result.scalar_one_or_none()
    if not fc and body.team_id:
        # Fall back to org-wide config
        fc_result = await db.execute(
            select(ForecastConfig).where(
                ForecastConfig.team_id.is_(None),
                ForecastConfig.is_active == True,
            )
        )
        fc = fc_result.scalar_one_or_none()

    if fc:
        if method == "auto":
            method = fc.method
        custom_ml_url = fc.custom_ml_url
        if fc.custom_ml_auth_encrypted:
            from orchestrator.core.credential_crypto import decrypt_credential
            try:
                custom_ml_auth = decrypt_credential(fc.custom_ml_auth_encrypted)
            except Exception as exc:
                logger.warning(
                    "Forecast: could not decrypt custom ML auth (check "
                    "MODUS_ENCRYPTION_KEY); proceeding without auth: %s", exc
                )

    # Weekday offset for seasonal adjustment
    weekday_offset = 0
    if daily_labels:
        try:
            first_date = datetime.strptime(daily_labels[0], "%Y-%m-%d")
            weekday_offset = first_date.weekday()
        except ValueError:
            pass

    # Run three-tier forecast
    forecast = await get_forecast(
        team_id=body.team_id or "org",
        team_name=team_name,
        daily_costs=daily_costs,
        daily_labels=daily_labels,
        mtd_actual=mtd_actual,
        budget_monthly=team_budget,
        days_remaining_month=days_remaining_month,
        days_remaining_quarter=days_remaining_quarter,
        days_remaining_year=days_remaining_year,
        weekday_offset=weekday_offset,
        provider_breakdown=provider_breakdown,
        forecast_method=method,
        custom_ml_url=custom_ml_url,
        custom_ml_auth=custom_ml_auth,
    )

    def _to_money_str(val: object) -> str:
        if val is None:
            return "0.00"
        return str(Decimal(str(val)).quantize(Decimal("0.01")))

    return ForecastResponse(
        team_id=body.team_id,
        team_name=team_name,
        forecast_eom=_to_money_str(forecast.get("forecast_eom", 0)),
        forecast_eoq=_to_money_str(forecast.get("forecast_eoq", 0)),
        forecast_eoy=_to_money_str(forecast.get("forecast_eoy", 0)),
        confidence_low_eom=_to_money_str(forecast.get("confidence_low_eom", 0)),
        confidence_high_eom=_to_money_str(forecast.get("confidence_high_eom", 0)),
        trend_daily=_to_money_str(forecast.get("trend_daily")) if forecast.get("trend_daily") is not None else None,
        trend=forecast.get("trend"),
        trend_pct_monthly=_to_money_str(forecast.get("trend_pct_monthly")) if forecast.get("trend_pct_monthly") is not None else None,
        method=forecast.get("method", "builtin"),
        r_squared=forecast.get("r_squared", 0),
        seasonal_factors=forecast.get("seasonal_factors", [1.0] * 7),
        breach_prediction=forecast.get("breach_prediction"),
        narrative=forecast.get("narrative"),
        patterns_detected=forecast.get("patterns_detected"),
        risk_level=forecast.get("risk_level"),
        mtd_actual=_to_money_str(mtd_actual),
        budget_monthly=team_budget,
        computed_at=now,
    )


# ── Budget Breach Prediction ────────────────────────────────────────────────


@router.get("/breach-predictions", response_model=list[BudgetBreachResponse])
async def get_breach_predictions(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Predict budget breaches across ALL teams with budgets.
    Returns only teams where a breach is predicted or already occurred.
    """
    from orchestrator.core.forecast_engine import predict_budget_breach

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=28)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # All teams with budgets
    teams_result = await db.execute(
        select(Team).where(
            Team.deleted_at.is_(None),
            Team.budget_monthly_usd.isnot(None),
            Team.budget_monthly_usd > 0,
        )
    )
    teams = teams_result.scalars().all()

    results: list[BudgetBreachResponse] = []

    for team in teams:
        tid = str(team.id)
        budget = Decimal(str(team.budget_monthly_usd))

        # Daily costs for last 28 days
        daily_result = await db.execute(text("""
            SELECT DATE(ua.period_start) AS day, SUM(ua.total_cost) AS cost
            FROM usage_aggregates ua
            WHERE ua.granularity = 'daily' AND ua.team_id = :tid
              AND ua.period_start >= :start AND ua.period_start < :end
            GROUP BY DATE(ua.period_start) ORDER BY DATE(ua.period_start)
        """), {"tid": tid, "start": sqlite_dt(window_start), "end": sqlite_dt(now)})
        daily_costs = [float(r.cost or 0) for r in daily_result.all()]

        # MTD
        mtd_result = await db.execute(text("""
            SELECT COALESCE(SUM(ua.total_cost), 0) FROM usage_aggregates ua
            WHERE ua.granularity = 'daily' AND ua.team_id = :tid
              AND ua.period_start >= :start AND ua.period_start < :end
        """), {"tid": tid, "start": sqlite_dt(month_start), "end": sqlite_dt(now)})
        mtd = Decimal(str(mtd_result.scalar() or 0))

        breach = predict_budget_breach(
            daily_costs=daily_costs,
            budget_monthly=float(budget),
            mtd_actual=float(mtd),
            day_of_month=now.day,
        )

        if breach:
            overage = breach.get("projected_overage")
            results.append(BudgetBreachResponse(
                team_id=tid,
                team_name=team.name,
                budget_monthly=str(budget.quantize(Decimal("0.01"))),
                mtd_actual=str(mtd.quantize(Decimal("0.01"))),
                breach_predicted=True,
                breach_date=breach.get("breach_date"),
                breach_day=breach.get("breach_day"),
                projected_overage=str(Decimal(str(overage)).quantize(Decimal("0.01"))) if overage is not None else None,
                confidence=breach.get("confidence"),
                already_breached=breach.get("already_breached", False),
            ))

    results.sort(key=lambda x: Decimal(x.projected_overage or "0"), reverse=True)
    return results


# ── Forecast Configuration (BYOML + AI) ─────────────────────────────────────


@router.get("/forecast/config", response_model=list[ForecastConfigResponse])
async def list_forecast_configs(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """List all forecast configurations."""

    result = await db.execute(
        select(ForecastConfig).where(ForecastConfig.is_active == True)
        .order_by(ForecastConfig.created_at.desc())
    )
    return [
        ForecastConfigResponse(
            id=str(fc.id),
            team_id=fc.team_id,
            method=fc.method,
            custom_ml_url=fc.custom_ml_url,
            has_ml_credentials=fc.custom_ml_auth_encrypted is not None,
            custom_ml_timeout_seconds=fc.custom_ml_timeout_seconds,
            is_active=fc.is_active,
            created_at=fc.created_at,
        )
        for fc in result.scalars().all()
    ]


@router.post("/forecast/config", response_model=ForecastConfigResponse, status_code=201)
async def upsert_forecast_config(
    body: ForecastConfigRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Create or update forecast configuration for a team or org-wide.

    Methods:
    - auto: Use AI if configured, else built-in
    - builtin: Pure Python forecasting (always available)
    - custom_ml: Your own ML endpoint (provide custom_ml_url)
    - ai: Your configured AI provider (MODUS_SUMMARY_AGENT)
    """

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    if body.method == "custom_ml" and not body.custom_ml_url:
        raise HTTPException(
            status_code=400,
            detail="custom_ml_url required when method is 'custom_ml'"
        )

    # Upsert: find existing config for this team
    existing_result = await db.execute(
        select(ForecastConfig).where(ForecastConfig.team_id == body.team_id)
    )
    fc = existing_result.scalar_one_or_none()

    # Encrypt ML auth if provided. Fails closed: if no MODUS_ENCRYPTION_KEY
    # is configured, refuse to persist plaintext.
    ml_auth_encrypted = None
    if body.custom_ml_auth:
        from orchestrator.core.credential_crypto import (
            CredentialEncryptionError,
            encrypt_credential,
        )
        try:
            ml_auth_encrypted = encrypt_credential(body.custom_ml_auth)
        except CredentialEncryptionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    if fc:
        fc.method = body.method
        fc.custom_ml_url = body.custom_ml_url
        if ml_auth_encrypted:
            fc.custom_ml_auth_encrypted = ml_auth_encrypted
        fc.custom_ml_timeout_seconds = body.custom_ml_timeout_seconds
        fc.is_active = True
    else:
        fc = ForecastConfig(
            team_id=body.team_id,
            method=body.method,
            custom_ml_url=body.custom_ml_url,
            custom_ml_auth_encrypted=ml_auth_encrypted,
            custom_ml_timeout_seconds=body.custom_ml_timeout_seconds,
            created_by=identity.actor_id,
        )
        db.add(fc)

    await db.flush()

    return ForecastConfigResponse(
        id=str(fc.id),
        team_id=fc.team_id,
        method=fc.method,
        custom_ml_url=fc.custom_ml_url,
        has_ml_credentials=fc.custom_ml_auth_encrypted is not None,
        custom_ml_timeout_seconds=fc.custom_ml_timeout_seconds,
        is_active=fc.is_active,
        created_at=fc.created_at,
    )


@router.delete("/forecast/config/{config_id}", status_code=204, response_model=None)
async def delete_forecast_config(
    config_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Delete a forecast configuration."""

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    fc = await db.get(ForecastConfig, config_id)
    if not fc:
        raise HTTPException(status_code=404, detail="Forecast config not found")
    await db.delete(fc)


# ── Provider Price Change Impact ─────────────────────────────────────────────


@router.post("/price-impact")
async def simulate_price_impact(
    body: PriceImpactRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Simulate the impact of a provider price change on monthly spend.
    E.g., "What if OpenAI raises prices 20%?" or "What if Anthropic cuts prices 15%?"
    """
    from orchestrator.core.forecast_engine import simulate_price_change
    result = await simulate_price_change(
        provider=body.provider,
        price_change_pct=body.price_change_pct,
        days=body.days,
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Price impact simulation failed")
    return result


# ── Scheduled Finance Reports ────────────────────────────────────────────────


class ReportCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    report_type: str = Field(..., pattern=r"^(chargeback|burn_rate|variance|audit_trail|forecast)$")
    schedule: str = Field(..., pattern=r"^(daily|weekly|monthly|quarterly)$")
    cost_center_id: Optional[str] = None
    delivery_channel: str = Field("webhook", pattern=r"^(webhook|slack)$")
    delivery_target: str = Field(..., min_length=1)
    format: str = Field("json", pattern=r"^(json|csv)$")


class ReportResponse(BaseModel):
    id: str
    name: str
    report_type: str
    schedule: str
    cost_center_id: Optional[str]
    delivery_channel: str
    delivery_target: str
    format: str
    is_active: bool
    last_run_at: Optional[datetime]
    created_by: Optional[str]
    created_at: datetime


@router.get("/reports", response_model=list[ReportResponse])
async def list_reports(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """List all scheduled finance reports."""

    result = await db.execute(
        select(FinanceReport).order_by(FinanceReport.created_at.desc())
    )
    return [
        ReportResponse(
            id=str(r.id), name=r.name, report_type=r.report_type,
            schedule=r.schedule, cost_center_id=r.cost_center_id,
            delivery_channel=r.delivery_channel,
            delivery_target=r.delivery_target, format=r.format,
            is_active=r.is_active, last_run_at=r.last_run_at,
            created_by=r.created_by, created_at=r.created_at,
        )
        for r in result.scalars().all()
    ]


@router.post("/reports", response_model=ReportResponse, status_code=201)
async def create_report(
    body: ReportCreateRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Create a scheduled finance report.

    Report types:
    - chargeback: Cost breakdown by team/cost-center/model
    - burn_rate: Budget burn rate with projections
    - variance: Month-over-month spend variance analysis
    - audit_trail: Compliance audit export
    - forecast: Spend forecast with breach predictions

    Delivery channels:
    - webhook: POST JSON/CSV to any URL (Slack, Teams, PagerDuty, custom)
    - slack: POST to Slack incoming webhook URL
    """

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    report = FinanceReport(
        name=body.name,
        report_type=body.report_type,
        schedule=body.schedule,
        cost_center_id=body.cost_center_id,
        delivery_channel=body.delivery_channel,
        delivery_target=body.delivery_target,
        format=body.format,
        created_by=identity.actor_id,
    )
    db.add(report)
    await db.flush()

    return ReportResponse(
        id=str(report.id), name=report.name, report_type=report.report_type,
        schedule=report.schedule, cost_center_id=report.cost_center_id,
        delivery_channel=report.delivery_channel,
        delivery_target=report.delivery_target, format=report.format,
        is_active=report.is_active, last_run_at=report.last_run_at,
        created_by=report.created_by, created_at=report.created_at,
    )


@router.delete("/reports/{report_id}", status_code=204, response_model=None)
async def delete_report(
    report_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Delete a scheduled report."""

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    report = await db.get(FinanceReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    await db.delete(report)


@router.post("/reports/{report_id}/toggle")
async def toggle_report(
    report_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Enable or disable a scheduled report."""

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    report = await db.get(FinanceReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    report.is_active = not report.is_active
    await db.flush()
    return {"id": str(report.id), "is_active": report.is_active}


@router.post("/reports/{report_id}/run")
async def run_report_now(
    report_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """Manually trigger a scheduled report immediately."""
    from orchestrator.core.report_scheduler import generate_and_deliver_report

    if not identity.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")
    report = await db.get(FinanceReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    result = await generate_and_deliver_report(report)
    return {
        "id": str(report.id),
        "status": "delivered" if result else "failed",
        "delivered_at": datetime.now(timezone.utc).isoformat(),
    }
