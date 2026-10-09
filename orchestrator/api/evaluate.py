"""
Modus — Pre-request Evaluation Gateway
================================================
The "standout" feature. Agents call POST /api/v1/evaluate BEFORE hitting
OpenAI/Anthropic/etc.

Returns allow/deny/throttle + estimated cost + suggested fallback model.

Ultra-low latency (~2-4 ms):
  1. In-memory token → cost estimation (no DB, ~0.01 ms)
  2. Real-time spend lookup (indexed point read, ~1 ms)
  3. Hierarchical budget check: session → app → team (cascading, ~1 ms)
  4. Full policy engine evaluation (reuses governance_policies, ~1 ms)

All deny/throttle decisions recorded to policy_decisions for audit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from orchestrator.core.auth import Identity, get_identity
from orchestrator.core.config import settings
from orchestrator.db.models import (
    App,
    RealTimeSpend,
    SessionBudget,
)
from orchestrator.db.session import get_session
from orchestrator.metrics.prometheus import POLICY_EVALUATE_DURATION_SECONDS

logger = logging.getLogger(__name__)

evaluate_router = APIRouter(prefix="/evaluate", tags=["enforcement"])


# ── Request / Response schemas ────────────────────────────────────────────────

class EvaluateRequest(BaseModel):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int = 0
    session_id: Optional[str] = None   # agent tracing — multi-step sessions
    app_id: str
    environment: str = "production"
    metadata: Optional[dict] = None


class EvaluateResponse(BaseModel):
    decision: str                          # allow | deny | throttle
    reason: str
    estimated_cost_usd: str                # Decimal serialized as string for precision
    retry_after_seconds: Optional[int] = None
    suggested_model: Optional[str] = None
    session_spend_usd: Optional[str] = None
    hierarchy: Optional[dict] = None       # {team_spend, app_spend, session_spend}


async def _discard_session(db: AsyncSession) -> None:
    """Roll back whatever the abandoned evaluation left in the session.

    A timed-out (cancelled) or failed evaluation can leave the session in an
    invalid transaction. get_session commits when the request finishes, so the
    fallback decision would otherwise turn into a 500 (PendingRollbackError).
    """
    try:
        await db.rollback()
    except Exception as exc:  # the fallback decision must still be returned
        logger.error("Evaluate: rollback after an abandoned evaluation failed: %s", exc)


# ── Endpoint ──────────────────────────────────────────────────────────────────

@evaluate_router.post("/", response_model=EvaluateResponse)
async def evaluate_request(
    req: EvaluateRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> EvaluateResponse:
    """
    Pre-request evaluation with enforced timeout.

    The timeout prevents enforcement from causing latency spikes in the
    calling application. If the timeout expires:
      - fail_open=True  → returns allow (availability > compliance)
      - fail_open=False → returns deny  (compliance > availability)
    """
    timeout_s = settings.enforcement_timeout_ms / 1000.0
    start = time.perf_counter()

    try:
        result = await asyncio.wait_for(
            _evaluate_inner(req, identity, db),
            timeout=timeout_s,
        )
        POLICY_EVALUATE_DURATION_SECONDS.labels(decision=result.decision).observe(
            time.perf_counter() - start
        )
        return result
    except asyncio.TimeoutError:
        await _discard_session(db)
        logger.warning(
            "Evaluate timeout (%dms) for app=%s provider=%s model=%s — %s",
            settings.enforcement_timeout_ms, req.app_id, req.provider, req.model,
            "fail-open (allow)" if settings.enforcement_fail_open else "fail-closed (deny)",
        )
        # Quick cost estimate (no DB, safe to run even after timeout)
        from orchestrator.core.pricing import estimate_cost
        _, _, est_cost = estimate_cost(
            req.provider, req.model, req.input_tokens, req.output_tokens
        )
        if settings.enforcement_fail_open:
            return EvaluateResponse(
                decision="allow",
                reason=f"Evaluation timed out ({settings.enforcement_timeout_ms}ms) — fail-open.",
                estimated_cost_usd=str(est_cost),
            )
        else:
            return EvaluateResponse(
                decision="deny",
                reason=f"Evaluation timed out ({settings.enforcement_timeout_ms}ms) — fail-closed.",
                estimated_cost_usd=str(est_cost),
            )
    except HTTPException:
        # Authorization failures must surface as 403 — never converted to a
        # fail-open allow by the blanket error handler below.
        raise
    except Exception as exc:
        await _discard_session(db)
        logger.error("Evaluate error for app=%s: %s", req.app_id, exc, exc_info=True)
        from orchestrator.core.pricing import estimate_cost
        _, _, est_cost = estimate_cost(
            req.provider, req.model, req.input_tokens, req.output_tokens
        )
        if settings.enforcement_fail_open:
            return EvaluateResponse(
                decision="allow",
                reason=f"Evaluation error (fail-open): {type(exc).__name__}",
                estimated_cost_usd=str(est_cost),
            )
        else:
            return EvaluateResponse(
                decision="deny",
                reason=f"Evaluation error (fail-closed): {type(exc).__name__}",
                estimated_cost_usd=str(est_cost),
            )


async def _evaluate_inner(
    req: EvaluateRequest,
    identity: Identity,
    db: AsyncSession,
) -> EvaluateResponse:
    """Core evaluation logic, wrapped by timeout in the endpoint handler."""
    t_start = time.perf_counter()

    # 0. Enforcement master switch
    if not settings.enforcement_enabled:
        from orchestrator.core.pricing import estimate_cost
        _, _, est_cost = estimate_cost(
            req.provider, req.model, req.input_tokens, req.output_tokens
        )
        return EvaluateResponse(
            decision="allow",
            reason="Enforcement disabled (master switch off).",
            estimated_cost_usd=str(est_cost),
        )

    # 1. Cheap in-memory token → cost estimation
    from orchestrator.core.pricing import estimate_cost
    est_input, est_output, est_cost = estimate_cost(
        req.provider, req.model, req.input_tokens, req.output_tokens
    )
    total_est = Decimal(str(est_cost))

    # 2. Load app — verify it exists and is active (eager-load team to avoid
    #    MissingGreenlet on lazy access in async context)
    app = await db.execute(
        select(App)
        .options(selectinload(App.team))
        .where(App.app_id == req.app_id, App.is_active == True)
    )
    app = app.scalar_one_or_none()
    if not app:
        return EvaluateResponse(
            decision="deny",
            reason="App not found or deactivated.",
            estimated_cost_usd=str(total_est),
        )

    # Authorization: the caller must belong to the app's team. Without this,
    # any authenticated identity could evaluate against any app_id and read
    # its spend hierarchy.
    identity.assert_team_access(str(app.team_id))

    if app.enforcement_state != "active":
        return EvaluateResponse(
            decision="deny",
            reason=f"App is {app.enforcement_state}: "
                   f"{app.enforcement_suspended_reason or 'Contact admin.'}",
            estimated_cost_usd=str(total_est),
        )

    # 3. Hierarchical budget checks: session → app → team (cascading)
    decision = "allow"
    reason = "Request permitted."
    retry_after = None
    suggested_model = None

    # 3a. Current app spend (daily window)
    now = datetime.now(timezone.utc)
    daily_key = f"daily:{now.strftime('%Y-%m-%d')}"
    app_uuid = str(app.id)
    rts_result = await db.execute(
        select(RealTimeSpend).where(
            RealTimeSpend.app_id == app_uuid,
            RealTimeSpend.period == "daily",
            RealTimeSpend.window_key == daily_key,
        )
    )
    rts = rts_result.scalar_one_or_none()
    app_spend = Decimal(str(rts.total_cost)) if rts else Decimal("0")

    # 3b. Session budget check (if session_id provided)
    session_spend = Decimal("0")
    session_budget_row = None
    if req.session_id:
        sb_result = await db.execute(
            select(SessionBudget).where(
                SessionBudget.session_id == req.session_id,
                SessionBudget.app_id == app_uuid,
            )
        )
        session_budget_row = sb_result.scalar_one_or_none()
        if session_budget_row:
            session_spend = session_budget_row.current_spend_usd
            if session_spend + total_est > session_budget_row.max_budget_usd:
                decision = "deny"
                reason = (
                    f"Session budget exceeded: "
                    f"${session_spend:.4f} + ${total_est:.4f} estimated "
                    f"> ${session_budget_row.max_budget_usd} cap."
                )

    # 3c. Team spend check (if team has max_budget_usd set)
    team_spend = Decimal("0")
    if hasattr(app.team, "max_budget_usd") and app.team and app.team.max_budget_usd:
        # Sum all app spends for the team in the current daily window
        team_rts = await db.execute(
            select(RealTimeSpend).where(
                RealTimeSpend.team_id == app.team_id,
                RealTimeSpend.period == "daily",
                RealTimeSpend.window_key == daily_key,
            )
        )
        for row in team_rts.scalars().all():
            team_spend += row.total_cost

        if team_spend + total_est > app.team.max_budget_usd and decision == "allow":
            decision = "deny"
            reason = (
                f"Team daily budget exceeded: "
                f"${team_spend:.4f} + ${total_est:.4f} estimated "
                f"> ${app.team.max_budget_usd} cap."
            )

    # 4. Run full policy engine (reuses existing governance_policies logic)
    if decision == "allow":
        from orchestrator.core.policy_engine import evaluate_policies
        policy_result = await evaluate_policies(
            db=db,
            app_id=app_uuid,
            team_id=app.team_id,
            provider=req.provider,
            model=req.model,
            environment=req.environment,
            estimated_tokens=req.input_tokens + req.output_tokens,
            estimated_cost=total_est,
        )
        if not policy_result.allowed:
            decision = policy_result.decision
            reason = policy_result.reason
            retry_after = policy_result.retry_after_seconds
            suggested_model = policy_result.suggested_model
        elif policy_result.suggested_model:
            # Degradation ladder: allow but recommend a cheaper model
            suggested_model = policy_result.suggested_model

    # 5. Update session spend atomically (if allowed and session_id set)
    if decision == "allow" and req.session_id and session_budget_row:
        session_budget_row.current_spend_usd += total_est
        await db.flush()

    # 6. Record non-allow decisions for audit (via write queue to avoid SQLite lock contention)
    if decision != "allow":
        from orchestrator.core.write_queue import enqueue, PolicyDecisionItem
        await enqueue(PolicyDecisionItem(
            app_id=app_uuid,
            team_id=app.team_id,
            decision=decision,
            reason=reason,
            request_provider=req.provider,
            request_model=req.model,
            request_environment=req.environment,
            request_estimated_tokens=req.input_tokens + req.output_tokens,
            request_estimated_cost=total_est,
            spend_at_decision=app_spend,
            evaluation_latency_ms=int((time.perf_counter() - t_start) * 1000),
        ))

    latency_ms = round((time.perf_counter() - t_start) * 1000, 2)
    logger.debug(
        "Evaluate complete",
        extra={
            "app_id": req.app_id,
            "decision": decision,
            "estimated_cost": str(total_est),
            "latency_ms": latency_ms,
        },
    )

    return EvaluateResponse(
        decision=decision,
        reason=reason,
        estimated_cost_usd=str(total_est),
        retry_after_seconds=retry_after,
        suggested_model=suggested_model,
        session_spend_usd=str(session_spend + total_est if decision == "allow" else session_spend) if req.session_id else None,
        hierarchy={
            "team_spend_usd": str(team_spend),
            "app_spend_usd": str(app_spend),
            "session_spend_usd": str(session_spend) if req.session_id else None,
        },
    )


# ── Sessions endpoint ─────────────────────────────────────────────────────────

class SessionBudgetCreate(BaseModel):
    session_id: str
    app_id: str
    max_budget_usd: str          # Decimal as string for precision


class SessionInfo(BaseModel):
    session_id: str
    app_id: str
    current_spend_usd: str       # Decimal serialized as string for precision
    max_budget_usd: str          # Decimal serialized as string for precision
    percent_used: float


@evaluate_router.post("/sessions", response_model=SessionInfo)
async def create_session_budget(
    req: SessionBudgetCreate,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> SessionInfo:
    """Create or update a session-level budget for agent tracing."""
    identity.assert_permission("evaluate:write")
    app = await db.execute(
        select(App).where(App.app_id == req.app_id, App.is_active == True)
    )
    app = app.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="App not found or deactivated.")
    identity.assert_team_access(str(app.team_id))

    app_uuid = str(app.id)
    existing = await db.execute(
        select(SessionBudget).where(
            SessionBudget.session_id == req.session_id,
            SessionBudget.app_id == app_uuid,
        )
    )
    sb = existing.scalar_one_or_none()

    if sb:
        sb.max_budget_usd = Decimal(str(req.max_budget_usd))
    else:
        sb = SessionBudget(
            session_id=req.session_id,
            app_id=app_uuid,
            team_id=app.team_id,
            max_budget_usd=Decimal(str(req.max_budget_usd)),
        )
        db.add(sb)
        await db.flush()

    return SessionInfo(
        session_id=sb.session_id,
        app_id=sb.app_id,
        current_spend_usd=str(sb.current_spend_usd),
        max_budget_usd=str(sb.max_budget_usd),
        percent_used=(
            float(sb.current_spend_usd / sb.max_budget_usd * 100)
            if sb.max_budget_usd > 0 else 0.0
        ),
    )


@evaluate_router.get("/sessions/active", response_model=list[SessionInfo])
async def list_active_sessions(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> list[SessionInfo]:
    """List all active session budgets (non-zero max_budget)."""
    identity.assert_permission("evaluate:read")
    q = select(SessionBudget).where(SessionBudget.max_budget_usd > 0)
    # Scope to user's teams unless platform admin
    if not identity.is_platform_admin and identity.team_ids:
        q = q.where(SessionBudget.team_id.in_(identity.team_ids))
    result = await db.execute(q)
    sessions = result.scalars().all()
    return [
        SessionInfo(
            session_id=s.session_id,
            app_id=s.app_id,
            current_spend_usd=str(s.current_spend_usd),
            max_budget_usd=str(s.max_budget_usd),
            percent_used=(
                float(s.current_spend_usd / s.max_budget_usd * 100)
                if s.max_budget_usd > 0 else 0.0
            ),
        )
        for s in sessions
    ]
