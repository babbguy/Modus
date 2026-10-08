"""
Modus — Governance Policies API
========================================
CRUD for governance policies + the synchronous evaluate endpoint.

POST /api/v1/policy/evaluate     — Pre-call enforcement check (agent-facing)
GET  /api/v1/policies            — List policies (admin-facing)
POST /api/v1/policies            — Create policy
PUT  /api/v1/policies/{id}       — Update policy
DELETE /api/v1/policies/{id}     — Deactivate policy (soft delete)
GET  /api/v1/policy/decisions    — Audit log of enforcement decisions
GET  /api/v1/policy/spend/{app_id} — Real-time spend counters for an app

The evaluate endpoint is the hot path. It is authenticated via the app's
mds_ key (same as ingest), not a user JWT. This means agents can call it
without any additional credential setup.

Latency target for /evaluate: <10ms p99 on a co-located database.
The response is intentionally minimal — only what the agent needs to act.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity, get_app_identity
from orchestrator.core.policy_engine import (
    EvaluateRequest,
    PolicyResult,
    evaluate,
)
from orchestrator.db.models import (
    App,
    AuditLog,
    GovernancePolicy,
    PolicyDecision,
    RealTimeSpend,
)
from orchestrator.db.session import get_session
from orchestrator.api.ingest import _verify_app_key

logger = logging.getLogger(__name__)
router = APIRouter()

# The ONE authoritative list of policy types. It is used by the single-policy
# API (PolicyCreate), the batch-apply API (BatchPolicyItem) and matches the
# evaluator branches in orchestrator/core/policy_engine.py. The stdlib-only
# SDK cannot import this module, so sdk/modus/policy_schema.py::POLICY_TYPES
# mirrors it by hand; tests/test_policy_types_consistency.py fails if the two
# ever drift.
VALID_POLICY_TYPES = {
    "budget_cap",
    "rate_limit",
    "model_allowlist",
    "model_denylist",
    "provider_block",
    "environment_block",
    "token_cap",
    "latency_cap",
    "degradation_ladder",
    "amplification_gate",
    "retry_circuit_breaker",
}

VALID_EFFECTS = {"deny", "throttle", "warn"}
VALID_SCOPES = {"platform", "team", "app"}


# ── Schemas ────────────────────────────────────────────────────────────────────

class EvaluateRequestBody(BaseModel):
    """What the agent sends before making an AI provider call."""
    provider: str = Field(..., max_length=64)
    model: Optional[str] = Field(None, max_length=256)
    environment: Optional[str] = Field(None, max_length=32)
    resource_type: Optional[str] = Field("llm_call", max_length=64)
    estimated_tokens: Optional[int] = Field(None, ge=0)
    estimated_cost: Optional[Decimal] = Field(None, ge=0)

    @field_validator("provider")
    @classmethod
    def lowercase_provider(cls, v: str) -> str:
        return v.lower().strip()


class EvaluateResponse(BaseModel):
    """Returned to the agent. Kept minimal — only what the agent needs."""
    decision: str           # "allow" | "deny" | "throttle"
    reason: str
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    suggested_model: Optional[str] = None
    message: Optional[str] = None


class PolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = None
    scope: str = Field(..., pattern="^(platform|team|app)$")
    policy_type: str
    effect: str = Field("deny", pattern="^(deny|throttle|warn)$")
    priority: int = Field(100, ge=1, le=999)
    team_id: Optional[str] = None
    app_id: Optional[str] = None
    conditions: Optional[dict] = None
    config: dict = Field(...)
    action: Optional[dict] = None

    @field_validator("policy_type")
    @classmethod
    def validate_policy_type(cls, v: str) -> str:
        if v not in VALID_POLICY_TYPES:
            raise ValueError(
                f"policy_type must be one of: {', '.join(sorted(VALID_POLICY_TYPES))}"
            )
        return v

    @field_validator("scope")
    @classmethod
    def validate_scope_anchors(cls, v: str) -> str:
        return v


class PolicyUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=256)
    description: Optional[str] = None
    effect: Optional[str] = Field(None, pattern="^(deny|throttle|warn)$")
    priority: Optional[int] = Field(None, ge=1, le=999)
    conditions: Optional[dict] = None
    config: Optional[dict] = None
    action: Optional[dict] = None
    is_active: Optional[bool] = None


class PolicyResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    scope: str
    policy_type: str
    effect: str
    priority: int
    team_id: Optional[str]
    app_id: Optional[str]
    conditions: Optional[dict]
    config: dict
    action: Optional[dict]
    is_active: bool
    created_by: str
    created_at: datetime
    updated_at: datetime


class PolicyDecisionResponse(BaseModel):
    id: str
    policy_id: Optional[str]
    policy_name: Optional[str]
    app_id: str
    team_id: str
    decision: str
    reason: str
    request_provider: Optional[str]
    request_model: Optional[str]
    request_environment: Optional[str]
    request_estimated_tokens: Optional[int]
    request_estimated_cost: Optional[Decimal]
    spend_at_decision: Optional[Decimal]
    spend_limit: Optional[Decimal]
    evaluation_latency_ms: Optional[int]
    decided_at: datetime


class RealTimeSpendResponse(BaseModel):
    app_id: str
    period: str
    window_key: str
    window_start: datetime
    window_end: datetime
    total_cost: Decimal
    call_count: int
    input_tokens: int
    output_tokens: int
    updated_at: datetime


# ── Evaluate endpoint (agent-facing, hot path) ─────────────────────────────────

@router.post(
    "/policy/evaluate",
    response_model=EvaluateResponse,
    status_code=200,
    summary="Pre-call enforcement check",
    description=(
        "Called by the agent before every AI provider call. "
        "Returns allow/deny/throttle with reason. "
        "Authenticated via app mds_ key (same as /ingest)."
    ),
    tags=["policy"],
)
async def policy_evaluate(
    body: EvaluateRequestBody,
    raw_key: str = Depends(get_app_identity),
    db: AsyncSession = Depends(get_session),
) -> EvaluateResponse:
    t_start = time.perf_counter()

    app = await _verify_app_key(raw_key, db)

    # Build engine request
    req = EvaluateRequest(
        app_id=str(app.id),
        team_id=str(app.team_id),
        provider=body.provider,
        model=body.model,
        environment=body.environment or app.environment,
        resource_type=body.resource_type,
        estimated_tokens=body.estimated_tokens,
        estimated_cost=body.estimated_cost,
    )

    result: PolicyResult = await evaluate(req, db)

    latency_ms = int((time.perf_counter() - t_start) * 1000)
    logger.info(
        "Policy evaluated",
        extra={
            "app_id": app.app_id,
            "provider": body.provider,
            "model": body.model,
            "decision": result.decision,
            "policy_id": result.policy_id,
            "latency_ms": latency_ms,
        },
    )

    return EvaluateResponse(
        decision=result.decision,
        reason=result.reason,
        policy_id=result.policy_id,
        policy_name=result.policy_name,
        retry_after_seconds=result.retry_after_seconds,
        suggested_model=result.suggested_model,
        message=result.custom_message,
    )


@router.get(
    "/policies",
    response_model=list[PolicyResponse],
    tags=["policy"],
    summary="List governance policies",
)
async def list_policies(
    scope: Optional[str] = None,
    team_id: Optional[str] = None,
    app_id: Optional[str] = None,
    active_only: bool = True,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[PolicyResponse]:
    q = select(GovernancePolicy)

    if active_only:
        q = q.where(GovernancePolicy.is_active == True)

    if scope:
        q = q.where(GovernancePolicy.scope == scope)

    identity.assert_permission("policies:read")
    if team_id:
        identity.assert_team_access(team_id)
        q = q.where(GovernancePolicy.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        # Non-admins can only see policies for their teams + platform policies
        q = q.where(
            (GovernancePolicy.team_id.in_(identity.team_ids))
            | GovernancePolicy.team_id.is_(None)
        )

    if app_id:
        q = q.where(GovernancePolicy.app_id == app_id)

    q = q.order_by(GovernancePolicy.scope, GovernancePolicy.priority)
    rows = (await db.execute(q)).scalars().all()

    return [
        PolicyResponse(
            id=str(p.id),
            name=p.name,
            description=p.description,
            scope=p.scope,
            policy_type=p.policy_type,
            effect=p.effect,
            priority=p.priority,
            team_id=str(p.team_id) if p.team_id else None,
            app_id=str(p.app_id) if p.app_id else None,
            conditions=p.conditions,
            config=p.config,
            action=p.action,
            is_active=p.is_active,
            created_by=p.created_by,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in rows
    ]


class PolicySyncResponse(BaseModel):
    policies: list[dict]
    app_id: str


@router.post(
    "/policies/sync",
    response_model=PolicySyncResponse,
    tags=["policy"],
    summary="Agent policy synchronization",
)
async def sync_policies(
    raw_key: str = Depends(get_app_identity),
    db: AsyncSession = Depends(get_session),
) -> PolicySyncResponse:
    """Agent-facing endpoint to fetch active policies for local enforcement."""
    app = await _verify_app_key(raw_key, db)

    # Fetch all active policies that apply to this app
    # (Platform scope + Team scope + App scope)
    q = select(GovernancePolicy).where(
        GovernancePolicy.is_active == True,
        (GovernancePolicy.scope == "platform") |
        ((GovernancePolicy.scope == "team") & (GovernancePolicy.team_id == app.team_id)) |
        ((GovernancePolicy.scope == "app") & (GovernancePolicy.app_id == app.id))
    ).order_by(GovernancePolicy.scope, GovernancePolicy.priority)

    rows = (await db.execute(q)).scalars().all()

    policies = [
        {
            "id": str(p.id),
            "name": p.name,
            "policy_type": p.policy_type,
            "effect": p.effect,
            "priority": p.priority,
            "config": p.config,
            "action": p.action,
            "suggested_model": p.suggested_model,
        }
        for p in rows
    ]

    return {"policies": policies, "app_id": str(app.id)}


@router.post(
    "/policies",
    response_model=PolicyResponse,
    status_code=201,
    tags=["policy"],
    summary="Create a governance policy",
)
async def create_policy(
    body: PolicyCreate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> PolicyResponse:
    identity.assert_permission("policies:write")

    # Scope validation
    if body.scope == "platform" and not identity.is_platform_admin:
        raise HTTPException(403, "Platform-scope policies require platform_admin role.")

    if body.scope in ("team", "app"):
        if not body.team_id:
            raise HTTPException(400, "team_id required for team and app scope policies.")
        identity.assert_team_access(body.team_id)

    if body.scope == "app" and not body.app_id:
        raise HTTPException(400, "app_id required for app-scope policies.")

    # Validate config has required keys for policy_type
    _validate_policy_config(body.policy_type, body.config)

    p = GovernancePolicy(
        name=body.name,
        description=body.description,
        scope=body.scope,
        policy_type=body.policy_type,
        effect=body.effect,
        priority=body.priority,
        team_id=body.team_id,
        app_id=body.app_id,
        conditions=body.conditions,
        config=body.config,
        action=body.action,
        is_active=True,
        created_by=identity.actor_id,
    )
    db.add(p)
    await db.flush()

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=body.team_id,
        resource_type="governance_policy",
        resource_id=str(p.id),
        action="created",
        after=body.model_dump(mode="json"),
    ))

    logger.info(
        "Policy created",
        extra={
            "policy_id": str(p.id),
            "policy_type": p.policy_type,
            "effect": p.effect,
            "scope": p.scope,
            "actor": identity.actor_id,
        },
    )

    return PolicyResponse(
        id=str(p.id), name=p.name, description=p.description,
        scope=p.scope, policy_type=p.policy_type, effect=p.effect,
        priority=p.priority, team_id=str(p.team_id) if p.team_id else None,
        app_id=str(p.app_id) if p.app_id else None,
        conditions=p.conditions, config=p.config, action=p.action,
        is_active=p.is_active, created_by=p.created_by,
        created_at=p.created_at, updated_at=p.updated_at,
    )


@router.put(
    "/policies/{policy_id}",
    response_model=PolicyResponse,
    tags=["policy"],
    summary="Update a governance policy",
)
async def update_policy(
    policy_id: str,
    body: PolicyUpdate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> PolicyResponse:
    identity.assert_permission("policies:write")

    p = await db.get(GovernancePolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy not found.")

    if p.scope == "platform" and not identity.is_platform_admin:
        raise HTTPException(403, "Platform-scope policies require platform_admin role.")

    if p.team_id:
        identity.assert_team_access(str(p.team_id))

    before = {
        "name": p.name, "effect": p.effect, "priority": p.priority,
        "config": p.config, "is_active": p.is_active,
    }

    if body.name is not None:
        p.name = body.name
    if body.description is not None:
        p.description = body.description
    if body.effect is not None:
        p.effect = body.effect
    if body.priority is not None:
        p.priority = body.priority
    if body.conditions is not None:
        p.conditions = body.conditions
    if body.config is not None:
        _validate_policy_config(p.policy_type, body.config)
        p.config = body.config
    if body.action is not None:
        p.action = body.action
    if body.is_active is not None:
        p.is_active = body.is_active

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=str(p.team_id) if p.team_id else None,
        resource_type="governance_policy",
        resource_id=policy_id,
        action="updated",
        before=before,
        after=body.model_dump(mode="json", exclude_none=True),
    ))

    return PolicyResponse(
        id=str(p.id), name=p.name, description=p.description,
        scope=p.scope, policy_type=p.policy_type, effect=p.effect,
        priority=p.priority, team_id=str(p.team_id) if p.team_id else None,
        app_id=str(p.app_id) if p.app_id else None,
        conditions=p.conditions, config=p.config, action=p.action,
        is_active=p.is_active, created_by=p.created_by,
        created_at=p.created_at, updated_at=p.updated_at,
    )


@router.delete(
    "/policies/{policy_id}",
    status_code=204,
    response_model=None,
    tags=["policy"],
    summary="Deactivate a governance policy",
)
async def deactivate_policy(
    policy_id: str,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> None:
    identity.assert_permission("policies:delete")

    p = await db.get(GovernancePolicy, policy_id)
    if not p:
        raise HTTPException(404, "Policy not found.")

    if p.scope == "platform" and not identity.is_platform_admin:
        raise HTTPException(403, "Platform-scope policies require platform_admin role.")

    if p.team_id:
        identity.assert_team_access(str(p.team_id))

    p.is_active = False

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=str(p.team_id) if p.team_id else None,
        resource_type="governance_policy",
        resource_id=policy_id,
        action="deactivated",
        before={"is_active": True},
        after={"is_active": False},
    ))


# ── Policy decisions audit log ─────────────────────────────────────────────────

@router.get(
    "/policy/decisions",
    response_model=list[PolicyDecisionResponse],
    tags=["policy"],
    summary="Enforcement decision audit log",
)
async def list_decisions(
    app_id: Optional[str] = None,
    team_id: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[PolicyDecisionResponse]:
    q = (
        select(PolicyDecision, GovernancePolicy.name.label("policy_name"))
        .outerjoin(
            GovernancePolicy,
            PolicyDecision.policy_id == GovernancePolicy.id,
        )
        .order_by(PolicyDecision.decided_at.desc())
        .limit(limit)
    )

    if app_id:
        q = q.where(PolicyDecision.app_id == app_id)

    if team_id:
        identity.assert_team_access(team_id)
        q = q.where(PolicyDecision.team_id == team_id)
    elif not identity.is_platform_admin and identity.team_ids:
        q = q.where(PolicyDecision.team_id.in_(identity.team_ids))

    if decision:
        q = q.where(PolicyDecision.decision == decision)

    rows = (await db.execute(q)).all()

    return [
        PolicyDecisionResponse(
            id=str(row.PolicyDecision.id),
            policy_id=str(row.PolicyDecision.policy_id) if row.PolicyDecision.policy_id else None,
            policy_name=row.policy_name,
            app_id=str(row.PolicyDecision.app_id),
            team_id=str(row.PolicyDecision.team_id),
            decision=row.PolicyDecision.decision,
            reason=row.PolicyDecision.reason,
            request_provider=row.PolicyDecision.request_provider,
            request_model=row.PolicyDecision.request_model,
            request_environment=row.PolicyDecision.request_environment,
            request_estimated_tokens=row.PolicyDecision.request_estimated_tokens,
            request_estimated_cost=row.PolicyDecision.request_estimated_cost,
            spend_at_decision=row.PolicyDecision.spend_at_decision,
            spend_limit=row.PolicyDecision.spend_limit,
            evaluation_latency_ms=row.PolicyDecision.evaluation_latency_ms,
            decided_at=row.PolicyDecision.decided_at,
        )
        for row in rows
    ]


# ── Real-time spend ────────────────────────────────────────────────────────────

@router.get(
    "/policy/spend/{app_id}",
    response_model=list[RealTimeSpendResponse],
    tags=["policy"],
    summary="Real-time spend counters for an app",
)
async def get_real_time_spend(
    app_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> list[RealTimeSpendResponse]:
    # Verify app exists and caller can see it
    app = await db.get(App, app_id)
    if not app:
        raise HTTPException(404, "App not found.")
    identity.assert_team_access(str(app.team_id))

    rows = (
        await db.execute(
            select(RealTimeSpend)
            .where(RealTimeSpend.app_id == app_id)
            .order_by(RealTimeSpend.period, RealTimeSpend.window_key)
        )
    ).scalars().all()

    return [
        RealTimeSpendResponse(
            app_id=str(r.app_id),
            period=r.period,
            window_key=r.window_key,
            window_start=r.window_start,
            window_end=r.window_end,
            total_cost=r.total_cost,
            call_count=r.call_count,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


# ── App enforcement state ──────────────────────────────────────────────────────

class EnforcementStateUpdate(BaseModel):
    enforcement_state: str = Field(
        ..., pattern="^(active|budget_suspended|rate_limited|admin_suspended)$"
    )
    reason: Optional[str] = Field(None, max_length=512)


@router.put(
    "/apps/{app_id}/enforcement",
    tags=["policy"],
    summary="Manually set enforcement state on an app",
)
async def set_enforcement_state(
    app_id: str,
    body: EnforcementStateUpdate,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
) -> dict:
    identity.assert_permission("policies:write")

    app = await db.get(App, app_id)
    if not app:
        raise HTTPException(404, "App not found.")

    identity.assert_team_access(str(app.team_id))

    prev_state = app.enforcement_state
    app.enforcement_state = body.enforcement_state
    app.enforcement_suspended_reason = body.reason
    app.enforcement_suspended_at = (
        datetime.now(timezone.utc)
        if body.enforcement_state != "active"
        else None
    )

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=str(app.team_id),
        resource_type="app",
        resource_id=app_id,
        action="enforcement_state_changed",
        before={"enforcement_state": prev_state},
        after={"enforcement_state": body.enforcement_state, "reason": body.reason},
    ))

    logger.warning(
        "App enforcement state changed",
        extra={
            "app_id": app.app_id,
            "from": prev_state,
            "to": body.enforcement_state,
            "reason": body.reason,
            "actor": identity.actor_id,
        },
    )

    return {
        "app_id": app_id,
        "enforcement_state": app.enforcement_state,
        "reason": body.reason,
    }


# ── Config validation helper ───────────────────────────────────────────────────

def _validate_policy_config(policy_type: str, config: dict) -> None:
    """Raise 400 if the config is missing required keys for the policy type."""
    required: dict[str, list[str]] = {
        "budget_cap": ["cap_usd", "period"],
        "rate_limit": ["max_calls", "window_seconds"],
        "model_allowlist": ["models"],
        "model_denylist": ["models"],
        "provider_block": ["providers"],
        "environment_block": ["environments"],
        "token_cap": ["max_tokens", "period"],
        "latency_cap": ["max_ms"],
        "degradation_ladder": ["budget_usd", "period", "tiers"],
        "amplification_gate": ["max_amplification"],
        "retry_circuit_breaker": ["max_retries"],
    }
    keys = required.get(policy_type, [])
    missing = [k for k in keys if k not in config]
    if missing:
        raise HTTPException(
            400,
            f"config is missing required keys for {policy_type}: {missing}",
        )

    # Validate period values where applicable
    if "period" in config and config["period"] not in ("hourly", "daily", "monthly"):
        raise HTTPException(
            400,
            "period must be one of: hourly, daily, monthly",
        )

    _validate_policy_config_values(policy_type, config)

    # Validate degradation_ladder tiers
    if policy_type == "degradation_ladder":
        tiers = config.get("tiers", [])
        if not isinstance(tiers, list) or len(tiers) < 1:
            raise HTTPException(400, "tiers must be a non-empty list")
        for i, tier in enumerate(tiers):
            if not isinstance(tier, dict):
                raise HTTPException(400, f"tiers[{i}] must be an object")
            pct = tier.get("pct")
            if "pct" in tier and (
                isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100
            ):
                raise HTTPException(400, f"tiers[{i}].pct must be a number between 0 and 100")
            if "pct" not in tier:
                raise HTTPException(400, f"tiers[{i}] missing required key 'pct'")
            if "model" not in tier and tier.get("action") != "deny":
                raise HTTPException(
                    400,
                    f"tiers[{i}] must have 'model' or 'action': 'deny'",
                )


_MONEY_KEYS = ("cap_usd", "budget_usd")
_POSITIVE_INT_KEYS = ("max_calls", "window_seconds", "max_tokens", "max_ms", "max_retries")
_LIST_KEYS = ("models", "providers", "environments")


def _validate_policy_config_values(policy_type: str, config: dict) -> None:
    """Reject config values the evaluator could not use (HTTP 400, never a crash at call time).

    Money fields (cap_usd, budget_usd) must be a positive decimal -- send a
    string such as "100.00"; they are handled as Decimal, never float.
    """
    from decimal import InvalidOperation

    for key in _MONEY_KEYS:
        if key in config:
            raw = config[key]
            try:
                if isinstance(raw, bool):
                    raise InvalidOperation
                amount = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                raise HTTPException(400, f"{key} must be a decimal amount such as \"100.00\"")
            if not amount.is_finite() or amount <= 0:
                raise HTTPException(400, f"{key} must be greater than 0")

    for key in _POSITIVE_INT_KEYS:
        if key in config:
            raw = config[key]
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise HTTPException(400, f"{key} must be a positive integer")

    for key in _LIST_KEYS:
        if key in config:
            raw = config[key]
            if (
                not isinstance(raw, list) or not raw
                or not all(isinstance(x, str) and x.strip() for x in raw)
            ):
                raise HTTPException(400, f"{key} must be a non-empty list of strings")

    if policy_type == "amplification_gate":
        raw = config.get("max_amplification")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 1:
            raise HTTPException(400, "max_amplification must be a number >= 1")

    if policy_type == "retry_circuit_breaker" and "window_minutes" in config:
        raw = config["window_minutes"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise HTTPException(400, "window_minutes must be a positive integer")


# ── Degradation Ladder Status ─────────────────────────────────────────────────

@router.get(
    "/policy/ladder-status",
    tags=["policy"],
    summary="Get degradation ladder status for all teams",
)
async def get_ladder_status(
    team_id: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session),
):
    """
    Returns the current degradation ladder status for each team with an active
    degradation_ladder policy.  Shows budget, current spend, percentage consumed,
    and which tier is active.
    """
    from datetime import datetime, timezone
    from decimal import Decimal
    from sqlalchemy import func as sa_func
    from orchestrator.core.policy_engine import _window_key

    now = datetime.now(timezone.utc)

    # Find all active degradation_ladder policies
    q = select(GovernancePolicy).where(
        GovernancePolicy.is_active == True,
        GovernancePolicy.policy_type == "degradation_ladder",
    )
    if team_id:
        q = q.where(GovernancePolicy.team_id == team_id)
    result = await db.execute(q)
    policies = result.scalars().all()

    statuses = []
    for policy in policies:
        cfg = policy.config or {}
        budget_usd = Decimal(str(cfg.get("budget_usd", "0")))
        period = cfg.get("period", "monthly")
        tiers = cfg.get("tiers", [])
        if not budget_usd or not tiers:
            continue

        wkey, _, _ = _window_key(period, now)

        # Aggregate spend for the team (or app)
        if policy.team_id and not policy.app_id:
            agg = await db.execute(
                select(sa_func.coalesce(sa_func.sum(RealTimeSpend.total_cost), 0)).where(
                    RealTimeSpend.team_id == policy.team_id,
                    RealTimeSpend.period == period,
                    RealTimeSpend.window_key == wkey,
                )
            )
            current_cost = Decimal(str(agg.scalar_one()))
        elif policy.app_id:
            r = await db.execute(
                select(RealTimeSpend).where(
                    RealTimeSpend.app_id == policy.app_id,
                    RealTimeSpend.period == period,
                    RealTimeSpend.window_key == wkey,
                )
            )
            rts = r.scalar_one_or_none()
            current_cost = rts.total_cost if rts else Decimal("0")
        else:
            continue  # platform-level not meaningful for status

        pct_used = float((current_cost / budget_usd) * 100) if budget_usd else 0.0

        # Find active tier
        sorted_tiers = sorted(tiers, key=lambda t: t.get("pct", 0), reverse=True)
        active_tier = None
        for tier in sorted_tiers:
            if pct_used >= tier.get("pct", 0):
                active_tier = tier
                break

        # Determine zone
        if active_tier is None:
            zone = "normal"
            active_model = None
        elif active_tier.get("action") == "deny":
            zone = "blocked"
            active_model = None
        else:
            zone = "degraded"
            active_model = active_tier.get("model")

        statuses.append({
            "policy_id": policy.id,
            "policy_name": policy.name,
            "scope": policy.scope,
            "team_id": policy.team_id,
            "app_id": policy.app_id,
            "period": period,
            "budget_usd": float(budget_usd),
            "current_spend_usd": float(current_cost),
            "pct_used": round(pct_used, 1),
            "zone": zone,
            "active_model": active_model,
            "active_tier_pct": active_tier.get("pct") if active_tier else None,
            "tiers": tiers,
        })

    return {"ladder_statuses": statuses}


# ── Budget-as-Code: Batch Apply ─────────────────────────────────────────────

class BatchPolicyItem(BaseModel):
    """A single policy from a modus-policy.yaml file."""
    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = None
    type: str
    # scope / effect default to None so the file-level ``defaults`` block can
    # supply them; the final fallbacks are applied in the handler.
    scope: Optional[str] = None
    effect: Optional[str] = None
    priority: Optional[int] = Field(100, ge=1, le=999)
    team: Optional[str] = None
    app: Optional[str] = None
    suggested_model: Optional[str] = None
    conditions: Optional[dict] = None
    config: Optional[dict] = None
    action: Optional[dict] = None
    enabled: Optional[bool] = True

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in VALID_POLICY_TYPES:
            raise ValueError(
                f"Invalid type: {v}. Must be one of: {', '.join(sorted(VALID_POLICY_TYPES))}"
            )
        return v

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_SCOPES:
            raise ValueError(f"scope must be one of: {', '.join(sorted(VALID_SCOPES))}")
        return v

    @field_validator("effect")
    @classmethod
    def validate_effect(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_EFFECTS:
            raise ValueError(f"effect must be one of: {', '.join(sorted(VALID_EFFECTS))}")
        return v


class BatchApplyRequest(BaseModel):
    """Request body for POST /api/v1/policies/apply."""
    version: str = "1"
    policies: list[BatchPolicyItem]
    defaults: Optional[dict] = None
    dry_run: bool = False


class BatchApplyResponse(BaseModel):
    summary: dict
    errors: list[str]
    policies: list[dict]


class _RefError(Exception):
    """A team/app reference in a policy file could not be resolved uniquely."""


@router.post(
    "/policies/apply",
    response_model=BatchApplyResponse,
    status_code=200,
    summary="Batch apply policies from YAML",
    description=(
        "Accepts a full policy file (Budget-as-Code format). Policies are "
        "matched by name: new names are created, changed ones updated, "
        "identical ones left alone, and active policies missing from the "
        "file are deactivated. The whole file is validated first; if any "
        "policy is invalid (unknown team/app, bad config) nothing is "
        "written and HTTP 422 lists every problem. `dry_run` reports the "
        "plan without writing. Teams are referenced by slug (preferred), "
        "exact name or UUID; apps by app_id or UUID."
    ),
)
async def batch_apply_policies(
    body: BatchApplyRequest,
    request: Request,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
):
    """Apply a batch of policies from a modus-policy.yaml file."""
    import uuid

    from orchestrator.db.models import Team

    identity.assert_permission("policies:write")

    defaults = body.defaults or {}
    if not isinstance(defaults, dict):
        raise HTTPException(422, "defaults must be an object.")

    names = [p.name for p in body.policies]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise HTTPException(422, {
            "message": "Policy file rejected; nothing was applied.",
            "errors": [f"duplicate policy name '{n}'." for n in dupes],
        })

    def _is_uuid(value: str) -> bool:
        try:
            uuid.UUID(value)
            return True
        except ValueError:
            return False

    async def resolve_team_id(ref: str) -> str:
        """Slug first, then UUID, then exact name. Raises _RefError."""
        team = (await session.execute(
            select(Team).where(Team.slug == ref, Team.deleted_at.is_(None))
        )).scalar_one_or_none()
        if team is not None:
            return str(team.id)
        if _is_uuid(ref):
            team = (await session.execute(
                select(Team).where(Team.id == str(uuid.UUID(ref)), Team.deleted_at.is_(None))
            )).scalar_one_or_none()
            if team is None:
                raise _RefError(f"team with id '{ref}' does not exist.")
            return str(team.id)
        matches = (await session.execute(
            select(Team).where(Team.name == ref, Team.deleted_at.is_(None))
        )).scalars().all()
        if not matches:
            raise _RefError(f"team '{ref}' not found (use the team slug, exact name or UUID).")
        if len(matches) > 1:
            slugs = ", ".join(sorted(t.slug for t in matches))
            raise _RefError(
                f"team name '{ref}' is ambiguous (matches slugs: {slugs}); use the slug."
            )
        return str(matches[0].id)

    async def resolve_app_id(ref: Optional[str], team_id: str) -> str:
        """app_id (the human-readable id) or UUID, within the team."""
        if not ref:
            raise _RefError("app scope requires 'app'.")
        q = select(App).where(App.team_id == team_id, App.deleted_at.is_(None))
        if _is_uuid(ref):
            q = q.where(App.id == str(uuid.UUID(ref)))
        else:
            q = q.where(App.app_id == ref)
        matches = (await session.execute(q)).scalars().all()
        if not matches:
            raise _RefError(f"app '{ref}' not found in the resolved team.")
        if len(matches) > 1:
            raise _RefError(f"app '{ref}' is ambiguous within the team; use its UUID.")
        return str(matches[0].id)

    # Existing policies visible to the caller. Inactive rows are included so a
    # policy disabled in the file is matched (and can be re-enabled) rather
    # than duplicated on every apply.
    q = select(GovernancePolicy).order_by(GovernancePolicy.updated_at.desc())
    if not identity.is_platform_admin:
        q = q.where(GovernancePolicy.team_id.in_(identity.team_ids or ["-"]))
    rows = (await session.execute(q)).scalars().all()
    existing: dict[str, GovernancePolicy] = {}
    for row in rows:
        current = existing.get(row.name)
        if current is None or (row.is_active and not current.is_active):
            existing[row.name] = row

    # ── Pass 1: validate + resolve everything, write nothing ───────────────
    errors: list[str] = []
    planned: list[dict] = []
    for i, item in enumerate(body.policies):
        prefix = f"policies[{i}] ({item.name})"
        scope = item.scope or defaults.get("scope") or "team"
        effect = item.effect or defaults.get("effect") or "deny"
        if scope not in VALID_SCOPES:
            errors.append(f"{prefix}: defaults.scope '{scope}' is invalid.")
            continue
        if effect not in VALID_EFFECTS:
            errors.append(f"{prefix}: defaults.effect '{effect}' is invalid.")
            continue

        config = item.config or {}
        try:
            _validate_policy_config(item.type, config)
        except HTTPException as exc:
            errors.append(f"{prefix}: {exc.detail}")
            continue

        team_id: Optional[str] = None
        app_id: Optional[str] = None
        try:
            if scope == "platform":
                if not identity.is_platform_admin:
                    raise _RefError("platform-scope policies require platform_admin.")
            else:
                team_ref = item.team or defaults.get("team")
                if not team_ref:
                    if len(identity.team_ids) == 1:
                        team_id = identity.team_ids[0]
                    else:
                        raise _RefError(
                            f"'{scope}' scope requires 'team' (set it or defaults.team)."
                        )
                else:
                    team_id = await resolve_team_id(str(team_ref))
                if not identity.can_access_team(team_id):
                    raise _RefError("you do not have access to that team.")
                if scope == "app":
                    app_id = await resolve_app_id(item.app, team_id)
        except _RefError as exc:
            errors.append(f"{prefix}: {exc}")
            continue

        planned.append({
            "item": item, "scope": scope, "effect": effect, "config": config,
            "team_id": team_id, "app_id": app_id,
            "enabled": True if item.enabled is None else item.enabled,
        })

    if errors:
        raise HTTPException(422, {
            "message": "Policy file rejected; nothing was applied.",
            "errors": errors,
        })

    # ── Pass 2: apply (or just plan, for dry_run) ──────────────────────────
    now = datetime.now(timezone.utc)
    created = updated = removed = unchanged = 0
    result_policies: list[dict] = []
    dry = body.dry_run

    for plan in planned:
        item = plan["item"]
        desired = {
            "description": item.description,
            "scope": plan["scope"],
            "policy_type": item.type,
            "effect": plan["effect"],
            "priority": item.priority or 100,
            "team_id": plan["team_id"],
            "app_id": plan["app_id"],
            "suggested_model": item.suggested_model,
            "conditions": item.conditions,
            "config": plan["config"],
            "action": item.action,
            "is_active": plan["enabled"],
        }
        p = existing.get(item.name)
        if p is None:
            created += 1
            if dry:
                result_policies.append({"name": item.name, "action": "create"})
                continue
            new_id = str(uuid.uuid4())
            session.add(GovernancePolicy(
                id=new_id, name=item.name,
                created_by=identity.actor_id or "policy-apply",
                **desired,
            ))
            result_policies.append({"name": item.name, "action": "created", "id": new_id})
            continue

        if all(getattr(p, field) == value for field, value in desired.items()):
            unchanged += 1
            result_policies.append({"name": item.name, "action": "unchanged", "id": str(p.id)})
            continue

        updated += 1
        if dry:
            result_policies.append({"name": item.name, "action": "update", "id": str(p.id)})
            continue
        for field, value in desired.items():
            setattr(p, field, value)
        p.updated_at = now
        result_policies.append({"name": item.name, "action": "updated", "id": str(p.id)})

    desired_names = {plan["item"].name for plan in planned}
    for name, p in existing.items():
        if name in desired_names or not p.is_active:
            continue
        removed += 1
        if dry:
            result_policies.append({"name": name, "action": "remove", "id": str(p.id)})
            continue
        p.is_active = False
        p.updated_at = now
        result_policies.append({"name": name, "action": "removed", "id": str(p.id)})

    if not dry:
        session.add(AuditLog(
            actor_id=identity.actor_id or "policy-apply",
            actor_ip=request.client.host if request.client else None,
            team_id=None,
            resource_type="governance_policy",
            resource_id=None,
            action="batch_applied",
            after={
                "created": created, "updated": updated, "removed": removed,
                "unchanged": unchanged, "policy_count": len(body.policies),
                "policies": [{"name": r["name"], "action": r["action"]} for r in result_policies],
            },
        ))
        await session.commit()

    return BatchApplyResponse(
        summary={"created": created, "updated": updated, "removed": removed,
                 "unchanged": unchanged},
        errors=[],
        policies=result_policies,
    )


# ── Budget-as-Code: Export ───────────────────────────────────────────────────

@router.get(
    "/policies/export",
    summary="Export policies as YAML-compatible JSON",
    description=(
        "Returns all active policies in modus-policy.yaml format. "
        "Pipe through `modus policy export > modus-policy.yaml` for YAML output."
    ),
)
async def export_policies(
    scope: Optional[str] = Query(None, pattern="^(platform|team|app)$"),
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
):
    """Export active policies in Budget-as-Code format."""
    q = select(GovernancePolicy).where(GovernancePolicy.is_active == True)

    if scope:
        q = q.where(GovernancePolicy.scope == scope)
    if identity.team_id:
        q = q.where(
            (GovernancePolicy.team_id == identity.team_id)
            | (GovernancePolicy.scope == "platform")
        )

    q = q.order_by(GovernancePolicy.priority.asc(), GovernancePolicy.name.asc())
    r = await session.execute(q)
    policies = r.scalars().all()

    # Resolve team/app names for export
    team_names: dict[str, str] = {}
    app_names: dict[str, str] = {}

    team_ids = {p.team_id for p in policies if p.team_id}
    if team_ids:
        from orchestrator.db.models import Team
        r = await session.execute(select(Team).where(Team.id.in_(team_ids)))
        for t in r.scalars().all():
            team_names[str(t.id)] = t.slug  # slug is the preferred stable reference

    app_ids = {p.app_id for p in policies if p.app_id}
    if app_ids:
        r = await session.execute(select(App).where(App.id.in_(app_ids)))
        for a in r.scalars().all():
            app_names[str(a.id)] = a.app_id  # app_id is the human-readable name

    # Build export
    exported: list[dict] = []
    for p in policies:
        entry: dict = {
            "name": p.name,
            "type": p.policy_type,
            "scope": p.scope,
            "effect": p.effect,
            "priority": p.priority,
        }
        if p.description:
            entry["description"] = p.description
        if p.team_id:
            entry["team"] = team_names.get(str(p.team_id), str(p.team_id))
        if p.app_id:
            entry["app"] = app_names.get(str(p.app_id), str(p.app_id))
        if p.suggested_model:
            entry["suggested_model"] = p.suggested_model
        if p.conditions:
            entry["conditions"] = p.conditions
        if p.config:
            entry["config"] = p.config
        if p.action:
            entry["action"] = p.action
        if not p.is_active:
            entry["enabled"] = False
        exported.append(entry)

    return {
        "version": "1",
        "policies": exported,
    }
