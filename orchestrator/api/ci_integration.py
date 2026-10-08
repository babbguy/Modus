"""
Modus — CI/CD Integration API (Phase 4e)
================================================
Two webhook endpoints for GitHub Actions and GitHub App integration:

POST /api/v1/ci/deployment-event    — Record a deployment, detect cost regression
POST /api/v1/ci/pr-cost-estimate    — Estimate cost impact of a PR's model changes

Both endpoints are authenticated via the standard API key mechanism.
No external dependencies — uses existing aggregation data and pricing table.

Deployment cost regression:
    After each deploy, CI posts the deployment metadata. Modus compares
    cost-per-request in the 1h window after deploy vs the 24h baseline before.
    If cost-per-request increases by > 20%, a warning is returned (and optionally
    an alert is fired).

PR cost impact:
    A GitHub App posts the list of model references in the PR diff. Modus
    looks up current vs proposed model pricing and returns the estimated
    monthly cost delta based on the app's current call volume.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import App, UsageAggregate
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ci", tags=["ci-integration"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class DeploymentEvent(BaseModel):
    app_id: str = Field(..., description="Modus app_id (slug)")
    environment: str = Field("production", description="Deployment environment")
    commit_sha: str = Field(..., min_length=7, max_length=40)
    deployed_at: Optional[datetime] = None
    deployer: Optional[str] = None
    run_url: Optional[str] = Field(None, description="GitHub Actions run URL")
    regression_threshold_pct: float = Field(
        20.0, ge=5.0, le=100.0,
        description="Cost-per-request increase % to flag as regression",
    )


class RegressionResult(BaseModel):
    app_id: str
    commit_sha: str
    status: str  # "ok" | "regression" | "insufficient_data"
    baseline_cost_per_request: Optional[float] = None
    post_deploy_cost_per_request: Optional[float] = None
    change_pct: Optional[float] = None
    message: str


class PrModelChange(BaseModel):
    # file_path is optional so callers that only have a plain list of model
    # references (e.g. the CI action's explicit-list mode, with no file context)
    # can post a clean payload without inventing a placeholder path.
    file_path: Optional[str] = None
    current_model: Optional[str] = None
    proposed_model: str


class PrCostRequest(BaseModel):
    app_id: str
    pr_number: int
    pr_url: Optional[str] = None
    model_changes: list[PrModelChange] = Field(..., min_length=1, max_length=50)


class PrModelImpact(BaseModel):
    file_path: Optional[str] = None
    current_model: Optional[str]
    proposed_model: str
    current_cost_per_1k: Optional[float]
    proposed_cost_per_1k: Optional[float]
    monthly_volume: int
    monthly_delta_usd: float


class PrCostResponse(BaseModel):
    app_id: str
    pr_number: int
    total_monthly_delta_usd: float
    model_impacts: list[PrModelImpact]
    message: str


# ── POST /ci/deployment-event ─────────────────────────────────────────────────

@router.post("/deployment-event", response_model=RegressionResult)
async def record_deployment(
    body: DeploymentEvent,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> RegressionResult:
    """
    Record a deployment and check for cost regression.

    Compares cost-per-request in the 1h window post-deploy against
    the 24h baseline before deploy. Returns a regression status.

    Called from GitHub Actions as a post-deploy step:
        curl -X POST $MODUS_URL/api/v1/ci/deployment-event \\
            -H "X-Modus-APIKey: $MDS_KEY" \\
            -d '{"app_id": "my-app", "commit_sha": "'$GITHUB_SHA'"}'
    """
    deploy_time = body.deployed_at or datetime.now(timezone.utc)

    # Look up the app
    app = (await db.execute(
        select(App).where(
            App.app_id == body.app_id,
            App.team_id == identity.team_id,
        )
    )).scalar_one_or_none()

    if not app:
        raise HTTPException(404, f"App '{body.app_id}' not found for your team.")

    app_uuid = str(app.id)

    # Baseline: 24h before deployment (hourly aggregates)
    baseline_start = deploy_time - timedelta(hours=24)
    baseline = await _cost_per_request(db, app_uuid, baseline_start, deploy_time)

    # Post-deploy: 1h window after deployment
    post_start = deploy_time
    post_end = deploy_time + timedelta(hours=1)

    # If deploy was very recent, use data up to now
    now = datetime.now(timezone.utc)
    if post_end > now:
        post_end = now

    post_deploy = await _cost_per_request(db, app_uuid, post_start, post_end)

    if baseline is None or post_deploy is None:
        return RegressionResult(
            app_id=body.app_id,
            commit_sha=body.commit_sha,
            status="insufficient_data",
            message="Not enough usage data for comparison. Check back after the app has been running for 1h post-deploy.",
        )

    change_pct = ((post_deploy - baseline) / baseline) * 100 if baseline > 0 else 0

    if change_pct > body.regression_threshold_pct:
        logger.warning(
            "Cost regression detected after deployment",
            extra={
                "app_id": body.app_id,
                "commit_sha": body.commit_sha,
                "change_pct": round(float(change_pct), 2),
            },
        )
        return RegressionResult(
            app_id=body.app_id,
            commit_sha=body.commit_sha,
            status="regression",
            baseline_cost_per_request=round(float(baseline), 6),
            post_deploy_cost_per_request=round(float(post_deploy), 6),
            change_pct=round(float(change_pct), 2),
            message=(
                f"Cost regression: cost-per-request increased {change_pct:.1f}% "
                f"(${baseline:.4f} -> ${post_deploy:.4f}) after commit {body.commit_sha[:7]}."
            ),
        )

    return RegressionResult(
        app_id=body.app_id,
        commit_sha=body.commit_sha,
        status="ok",
        baseline_cost_per_request=round(float(baseline), 6),
        post_deploy_cost_per_request=round(float(post_deploy), 6),
        change_pct=round(float(change_pct), 2),
        message="No cost regression detected.",
    )


async def _cost_per_request(
    db: AsyncSession,
    app_uuid: str,
    start: datetime,
    end: datetime,
) -> Optional[Decimal]:
    """Compute average cost per request from hourly aggregates."""
    result = await db.execute(
        select(
            func.coalesce(func.sum(UsageAggregate.total_cost), 0),
            func.coalesce(func.sum(UsageAggregate.call_count), 0),
        ).where(
            UsageAggregate.app_id == app_uuid,
            UsageAggregate.granularity == "hourly",
            UsageAggregate.period_start >= start,
            UsageAggregate.period_start < end,
        )
    )
    row = result.one()
    total_cost = Decimal(str(row[0]))
    total_calls = int(row[1])

    if total_calls < 10:  # Minimum sample size
        return None

    return total_cost / total_calls


# ── POST /ci/pr-cost-estimate ────────────────────────────────────────────────

@router.post("/pr-cost-estimate", response_model=PrCostResponse)
async def pr_cost_estimate(
    body: PrCostRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> PrCostResponse:
    """
    Estimate cost impact of model changes in a PR.

    A GitHub App or CI script parses the PR diff for model string changes
    (e.g., "gpt-4" → "gpt-4o-mini") and posts them here. Modus returns
    the estimated monthly cost delta based on current call volume.

    Useful for PR review: "This change will save $142/month" or
    "Warning: this change will increase costs by $890/month".
    """
    # Look up the app
    app = (await db.execute(
        select(App).where(
            App.app_id == body.app_id,
            App.team_id == identity.team_id,
        )
    )).scalar_one_or_none()

    if not app:
        raise HTTPException(404, f"App '{body.app_id}' not found for your team.")

    app_uuid = str(app.id)

    # Get current monthly volume per model (last 30 days)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    volume_result = await db.execute(
        select(
            UsageAggregate.model,
            func.sum(UsageAggregate.call_count).label("calls"),
            func.sum(UsageAggregate.input_tokens).label("input_tokens"),
            func.sum(UsageAggregate.output_tokens).label("output_tokens"),
        ).where(
            UsageAggregate.app_id == app_uuid,
            UsageAggregate.granularity == "daily",
            UsageAggregate.period_start >= thirty_days_ago,
        ).group_by(UsageAggregate.model)
    )
    volume_by_model = {r.model: {
        "calls": int(r.calls or 0),
        "input_tokens": int(r.input_tokens or 0),
        "output_tokens": int(r.output_tokens or 0),
    } for r in volume_result.all()}

    # Load pricing table
    pricing = await _load_pricing(db)

    impacts = []
    total_delta = Decimal("0")

    for change in body.model_changes:
        current_model = change.current_model
        proposed_model = change.proposed_model

        # Use volume from current model, or proposed model if new
        vol = volume_by_model.get(current_model, volume_by_model.get(proposed_model, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
        }))

        current_cost = _estimate_monthly_cost(pricing, current_model, vol) if current_model else Decimal("0")
        proposed_cost = _estimate_monthly_cost(pricing, proposed_model, vol)
        delta = proposed_cost - current_cost

        impacts.append(PrModelImpact(
            file_path=change.file_path,
            current_model=current_model,
            proposed_model=proposed_model,
            current_cost_per_1k=float(_cost_per_1k(pricing, current_model)) if current_model else None,
            proposed_cost_per_1k=float(_cost_per_1k(pricing, proposed_model)),
            monthly_volume=vol["calls"],
            monthly_delta_usd=round(float(delta), 2),
        ))
        total_delta += delta

    if total_delta > 0:
        msg = f"This PR will increase monthly costs by ${float(total_delta):.2f}."
    elif total_delta < 0:
        msg = f"This PR will save ${abs(float(total_delta)):.2f}/month."
    else:
        msg = "No cost impact detected."

    return PrCostResponse(
        app_id=body.app_id,
        pr_number=body.pr_number,
        total_monthly_delta_usd=round(float(total_delta), 2),
        model_impacts=impacts,
        message=msg,
    )


async def _load_pricing(db: AsyncSession) -> dict[str, dict]:
    """Load pricing table from DB into a lookup dict keyed by model name."""
    from orchestrator.db.models import PricingModel
    rows = (await db.execute(select(PricingModel))).scalars().all()
    pricing = {}
    for r in rows:
        pricing[r.model] = {
            "input_per_1k": Decimal(str(r.input_cost_per_1k)) if r.input_cost_per_1k else Decimal("0"),
            "output_per_1k": Decimal(str(r.output_cost_per_1k)) if r.output_cost_per_1k else Decimal("0"),
        }
    return pricing


def _cost_per_1k(pricing: dict, model: Optional[str]) -> Decimal:
    """Blended cost per 1K tokens (50/50 input/output split estimate)."""
    if not model or model not in pricing:
        return Decimal("0")
    p = pricing[model]
    return (p["input_per_1k"] + p["output_per_1k"]) / 2


def _estimate_monthly_cost(
    pricing: dict,
    model: Optional[str],
    volume: dict,
) -> Decimal:
    """Estimate monthly cost from pricing and token volume."""
    if not model or model not in pricing:
        return Decimal("0")
    p = pricing[model]
    input_cost = p["input_per_1k"] * volume["input_tokens"] / 1000
    output_cost = p["output_per_1k"] * volume["output_tokens"] / 1000
    return input_cost + output_cost
