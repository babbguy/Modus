"""
Modus — Policy Engine
==============================
Synchronous evaluation of governance policies before AI calls.

This is the enforcement hinge. Every AI call from an instrumented application
passes through evaluate() before hitting the provider. The result is one of:

    allow    — proceed normally
    deny     — blocked; agent raises PolicyViolationError
    throttle — blocked temporarily; agent sleeps retry_after_seconds and retries

Evaluation order:
    1.  App enforcement_state check (budget_suspended / admin_suspended / rate_limited)
        -- terminal; short-circuits everything below.
    2.  ALL applicable policies are evaluated, in this order:
        app-scope, then team-scope, then platform-scope (priority ASC within a scope).
    3.  Results are combined, most restrictive wins:
        deny > throttle > allow-with-downshift (degradation ladder) > allow.
        A ladder downshift never bypasses a deny/throttle from any other policy.
        "warn" policies are recorded but never change the outcome.

Within each scope, lower priority number = evaluated first.
Policy conditions are AND-evaluated — all specified conditions must match.

Performance:
    evaluate() is called once per AI call, synchronously.
    It does two indexed reads: one on apps (PK lookup), one on
    governance_policies (ix_policies_active + scope/app/team filters).
    Both return small result sets. p99 latency target: <5ms on co-located DB.

Budget cap evaluation:
    Reads real_time_spend for the relevant period window.
    This is an indexed point lookup (uq_rts_app_window). Sub-millisecond.

All deny/throttle/warn decisions are written to policy_decisions (async,
non-blocking relative to the response — fire-and-forget after response is sent).
"""

from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import App, GovernancePolicy, PolicyDecision, RealTimeSpend

logger = logging.getLogger(__name__)


# ── Request context ────────────────────────────────────────────────────────────

@dataclass
class EvaluateRequest:
    """
    What the agent sends before making an AI provider call.
    All fields except provider are optional — the engine degrades gracefully
    when the agent can't estimate tokens before the call.
    """
    app_id: str           # App UUID (from DB, resolved from API key by ingest)
    team_id: str          # Team UUID (denormalized on App)
    provider: str         # "anthropic" | "openai" | "bedrock" | etc.
    model: Optional[str] = None
    environment: Optional[str] = None        # "production" | "staging" | "dev"
    resource_type: Optional[str] = "llm_call"
    estimated_tokens: Optional[int] = None   # best-effort pre-call estimate
    estimated_cost: Optional[Decimal] = None # best-effort pre-call estimate


# ── Decision ───────────────────────────────────────────────────────────────────

@dataclass
class PolicyResult:
    """
    Returned to the agent and written to policy_decisions.
    """
    decision: str               # "allow" | "deny" | "throttle"
    reason: str                 # human-readable, shown to developer in logs
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    retry_after_seconds: Optional[int] = None   # for throttle
    suggested_model: Optional[str] = None       # agent may honour this
    custom_message: Optional[str] = None        # from policy.action.message
    spend_at_decision: Optional[Decimal] = None
    spend_limit: Optional[Decimal] = None

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


_ALLOW = PolicyResult(decision="allow", reason="No matching policy.")


# ── Current window helpers ─────────────────────────────────────────────────────

def _window_key(period: str, now: datetime) -> tuple[str, datetime, datetime]:
    """
    Return (window_key, window_start, window_end) for the given period.
    window_key is a stable string that uniquely identifies the window.
    """
    if period == "hourly":
        start = now.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        key = f"hourly:{start.strftime('%Y-%m-%dT%H')}"
    elif period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        key = f"daily:{start.strftime('%Y-%m-%d')}"
    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # End: first day of next month
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        key = f"monthly:{start.strftime('%Y-%m')}"
    else:
        raise ValueError(f"Unknown period: {period!r}")
    return key, start, end


# ── Condition matching ─────────────────────────────────────────────────────────

def _conditions_match(conditions: Optional[dict], req: EvaluateRequest) -> bool:
    """
    Return True if all conditions in the policy match the request.
    Missing conditions = wildcard (always match).
    All conditions must match (AND logic).
    """
    if not conditions:
        return True

    # providers: list of provider strings
    if "providers" in conditions:
        if req.provider not in conditions["providers"]:
            return False

    # model_pattern: glob string e.g. "gpt-4*", "claude-opus*"
    if "model_pattern" in conditions:
        if not req.model:
            return False  # no model specified, pattern can't match
        if not fnmatch.fnmatch(req.model.lower(), conditions["model_pattern"].lower()):
            return False

    # environments: list of environment strings
    if "environments" in conditions:
        if req.environment not in conditions["environments"]:
            return False

    # resource_types: list of resource type strings
    if "resource_types" in conditions:
        rt = req.resource_type or "llm_call"
        if rt not in conditions["resource_types"]:
            return False

    return True


# ── Per-policy evaluation ──────────────────────────────────────────────────────

async def _evaluate_policy(
    policy: GovernancePolicy,
    req: EvaluateRequest,
    db: AsyncSession,
    now: datetime,
) -> Optional[PolicyResult]:
    """
    Evaluate one policy against the request.
    Returns PolicyResult if the policy matches and fires, None if it doesn't match.
    """
    cfg = policy.config or {}
    action = policy.action or {}

    # ── 1. Check conditions first (cheap, no DB) ───────────────────────────────
    if not _conditions_match(policy.conditions, req):
        return None

    # ── 2. Policy-type specific evaluation ────────────────────────────────────

    if policy.policy_type == "model_allowlist":
        allowed_models = cfg.get("models", [])
        if not req.model or req.model not in allowed_models:
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"Model '{req.model}' is not in the allowlist for this "
                    f"{'app' if policy.scope == 'app' else policy.scope}. "
                    f"Allowed: {', '.join(allowed_models) or 'none configured'}."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                suggested_model=action.get("suggested_model"),
                custom_message=action.get("message"),
            )
        return None  # model is allowed

    if policy.policy_type == "model_denylist":
        denied_models = cfg.get("models", [])
        if req.model and req.model in denied_models:
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"Model '{req.model}' is explicitly blocked by policy "
                    f"'{policy.name}'."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                suggested_model=action.get("suggested_model"),
                custom_message=action.get("message"),
            )
        return None

    if policy.policy_type == "provider_block":
        blocked = cfg.get("providers", [])
        if req.provider in blocked:
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"Provider '{req.provider}' is blocked by policy '{policy.name}'."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                custom_message=action.get("message"),
            )
        return None

    if policy.policy_type == "environment_block":
        blocked_envs = cfg.get("environments", [])
        if req.environment and req.environment in blocked_envs:
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"Environment '{req.environment}' is blocked by policy "
                    f"'{policy.name}'. This model/provider is not permitted "
                    f"in {req.environment}."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                suggested_model=action.get("suggested_model"),
                custom_message=action.get("message"),
            )
        return None

    if policy.policy_type == "token_cap":
        if req.estimated_tokens is None:
            return None  # can't evaluate without an estimate — let it through
        period = cfg.get("period", "daily")
        max_tokens = int(cfg.get("max_tokens", 0))
        if not max_tokens:
            return None
        # Read real-time token usage
        wkey, wstart, wend = _window_key(period, now)
        result = await db.execute(
            select(RealTimeSpend).where(
                RealTimeSpend.app_id == req.app_id,
                RealTimeSpend.period == period,
                RealTimeSpend.window_key == wkey,
            )
        )
        rts = result.scalar_one_or_none()
        current_tokens = int(rts.input_tokens + rts.output_tokens) if rts else 0
        projected = current_tokens + req.estimated_tokens
        if projected > max_tokens:
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"Token cap exceeded. {period.capitalize()} limit: "
                    f"{max_tokens:,} tokens. Current: {current_tokens:,}. "
                    f"Estimated call: {req.estimated_tokens:,}."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                retry_after_seconds=action.get("retry_after_seconds"),
                custom_message=action.get("message"),
            )
        return None

    if policy.policy_type == "budget_cap":
        period = cfg.get("period", "daily")
        cap_usd = Decimal(str(cfg.get("cap_usd", "0")))
        if not cap_usd:
            return None
        wkey, wstart, wend = _window_key(period, now)
        result = await db.execute(
            select(RealTimeSpend).where(
                RealTimeSpend.app_id == req.app_id,
                RealTimeSpend.period == period,
                RealTimeSpend.window_key == wkey,
            )
        )
        rts = result.scalar_one_or_none()
        current_cost = rts.total_cost if rts else Decimal("0")
        projected = current_cost + (req.estimated_cost or Decimal("0"))

        if projected >= cap_usd or current_cost >= cap_usd:
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"{period.capitalize()} budget cap of ${cap_usd} exceeded. "
                    f"Current spend: ${current_cost:.4f}."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                retry_after_seconds=action.get(
                    "retry_after_seconds",
                    int((wend - now).total_seconds()) if policy.effect == "throttle" else None,
                ),
                suggested_model=action.get("suggested_model"),
                custom_message=action.get("message"),
                spend_at_decision=current_cost,
                spend_limit=cap_usd,
            )
        return None

    if policy.policy_type == "rate_limit":
        max_calls = int(cfg.get("max_calls", 0))
        window_seconds = int(cfg.get("window_seconds", 3600))
        if not max_calls:
            return None
        # Reject sub-hour windows: RealTimeSpend only tracks hourly/daily/monthly
        # buckets, so sub-hour enforcement is not possible. Treat as a config
        # error and deny with a clear message so the admin fixes the policy.
        if window_seconds < 3600:
            logger.error(
                "Policy %s has window_seconds=%d (< 3600). Sub-hour rate limits "
                "are not supported — minimum granularity is 1 hour (3600s). "
                "Skipping this policy.",
                policy.id, window_seconds,
            )
            return None
        # Map window_seconds to a period for real_time_spend lookup
        if window_seconds <= 3600:
            period = "hourly"
        elif window_seconds <= 86400:
            period = "daily"
        else:
            period = "monthly"
        wkey, wstart, wend = _window_key(period, now)
        result = await db.execute(
            select(RealTimeSpend).where(
                RealTimeSpend.app_id == req.app_id,
                RealTimeSpend.period == period,
                RealTimeSpend.window_key == wkey,
            )
        )
        rts = result.scalar_one_or_none()
        current_calls = rts.call_count if rts else 0
        if current_calls >= max_calls:
            retry_after = action.get("retry_after_seconds", window_seconds)
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"Rate limit of {max_calls:,} calls per "
                    f"{window_seconds}s exceeded. "
                    f"Current: {current_calls:,} calls."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                retry_after_seconds=retry_after,
                custom_message=action.get("message"),
            )
        return None

    if policy.policy_type == "latency_cap":
        max_ms = int(cfg.get("max_ms", 0))
        period = cfg.get("period", "hourly")  # Defaults to hourly for responsiveness
        if not max_ms:
            return None

        # Read real-time latency data
        wkey, wstart, wend = _window_key(period, now)
        result = await db.execute(
            select(RealTimeSpend).where(
                RealTimeSpend.app_id == req.app_id,
                RealTimeSpend.period == period,
                RealTimeSpend.window_key == wkey,
            )
        )
        rts = result.scalar_one_or_none()
        if not rts or rts.call_count == 0:
            return None  # No history to evaluate

        avg_latency = rts.total_duration_ms / rts.call_count
        if avg_latency > max_ms:
            return PolicyResult(
                decision=policy.effect,
                reason=(
                    f"Average latency ({avg_latency:.0f}ms) exceeds the cap of {max_ms}ms "
                    f"for the current {period} window."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                suggested_model=action.get("suggested_model"),
                custom_message=action.get("message"),
                spend_at_decision=rts.total_cost,
            )
        return None

    if policy.policy_type == "degradation_ladder":
        # Progressive model downshift based on budget consumption percentage.
        # Config:
        #   budget_usd: total budget for the period
        #   period: "hourly" | "daily" | "monthly"
        #   tiers: [{"pct": 70, "model": "claude-sonnet-4-5-20251022"},
        #           {"pct": 90, "model": "claude-haiku-4-5-20251001"},
        #           {"pct": 100, "action": "deny"}]
        # Tiers must be sorted by pct ascending.  The engine finds the highest
        # tier whose pct threshold has been reached and returns the appropriate
        # suggested_model (or deny at 100%).
        budget_usd = Decimal(str(cfg.get("budget_usd", "0")))
        if not budget_usd:
            return None
        period = cfg.get("period", "monthly")
        tiers = cfg.get("tiers", [])
        if not tiers:
            return None

        wkey, wstart, wend = _window_key(period, now)

        # For team/platform scope, aggregate spend across all apps in the team.
        if policy.scope in ("team", "platform") and req.team_id:
            from sqlalchemy import func as sa_func
            agg_result = await db.execute(
                select(sa_func.coalesce(sa_func.sum(RealTimeSpend.total_cost), 0)).where(
                    RealTimeSpend.team_id == req.team_id,
                    RealTimeSpend.period == period,
                    RealTimeSpend.window_key == wkey,
                )
            )
            current_cost = Decimal(str(agg_result.scalar_one()))
        else:
            result = await db.execute(
                select(RealTimeSpend).where(
                    RealTimeSpend.app_id == req.app_id,
                    RealTimeSpend.period == period,
                    RealTimeSpend.window_key == wkey,
                )
            )
            rts = result.scalar_one_or_none()
            current_cost = rts.total_cost if rts else Decimal("0")

        pct_used = (current_cost / budget_usd) * 100 if budget_usd else Decimal("0")

        # Walk tiers from highest to lowest — find the active tier
        sorted_tiers = sorted(tiers, key=lambda t: t.get("pct", 0), reverse=True)
        active_tier = None
        for tier in sorted_tiers:
            if pct_used >= Decimal(str(tier.get("pct", 0))):
                active_tier = tier
                break

        if active_tier is None:
            return None  # Under all thresholds — allow normally

        tier_action = active_tier.get("action", "downshift")
        tier_model = active_tier.get("model")
        tier_pct = active_tier.get("pct", 0)

        if tier_action == "deny":
            return PolicyResult(
                decision="deny",
                reason=(
                    f"Budget fully consumed ({pct_used:.1f}% of "
                    f"${budget_usd} {period} budget). "
                    f"Current spend: ${current_cost:.4f}."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
                custom_message=action.get("message"),
                spend_at_decision=current_cost,
                spend_limit=budget_usd,
            )

        # Downshift: if the request is already using the tier model (or cheaper),
        # allow it through.  Otherwise, suggest the downshift.
        if req.model and req.model == tier_model:
            return None  # Already on the right model

        return PolicyResult(
            decision="allow",
            reason=(
                f"Degradation ladder active: {pct_used:.1f}% of "
                f"${budget_usd} {period} budget consumed. "
                f"Tier {tier_pct}% recommends model '{tier_model}'."
            ),
            policy_id=policy.id,
            policy_name=policy.name,
            suggested_model=tier_model,
            custom_message=action.get("message", f"Budget at {pct_used:.0f}% — downshifting to {tier_model}."),
            spend_at_decision=current_cost,
            spend_limit=budget_usd,
        )

    # ── Phase 5: Attribution-based policies ──────────────────────────────────

    if policy.policy_type == "amplification_gate":
        # Deny/warn when a node's amplification factor exceeds a threshold.
        # Config: {"max_amplification": 5.0, "action": "deny"|"warn"}
        max_af = float(cfg.get("max_amplification", 5.0))
        gate_action = cfg.get("action", "warn")

        # Look up the app's recent max amplification factor
        from orchestrator.db.models import AttributionNode
        from sqlalchemy import func as sa_func
        af_result = await db.execute(
            select(sa_func.max(AttributionNode.amplification_factor)).where(
                AttributionNode.app_id == req.app_id,
                AttributionNode.created_at >= now - timedelta(hours=1),
            )
        )
        recent_max_af = af_result.scalar() or 0.0

        if recent_max_af > max_af:
            decision = "deny" if gate_action == "deny" else "throttle"
            return PolicyResult(
                decision=decision,
                reason=(
                    f"Amplification factor {recent_max_af:.1f}x exceeds "
                    f"limit {max_af:.1f}x. Reduce output verbosity in high-AF nodes."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
            )
        return None

    if policy.policy_type == "retry_circuit_breaker":
        # Deny when retry count for recent sessions exceeds threshold.
        # Config: {"max_retries": 3, "window_minutes": 5}
        max_retries = int(cfg.get("max_retries", 3))
        window_min = int(cfg.get("window_minutes", 5))

        from orchestrator.db.models import AttributionNode
        from sqlalchemy import func as sa_func
        retry_result = await db.execute(
            select(sa_func.count(AttributionNode.id)).where(
                AttributionNode.app_id == req.app_id,
                AttributionNode.is_retry == True,
                AttributionNode.created_at >= now - timedelta(minutes=window_min),
            )
        )
        retry_count = retry_result.scalar() or 0

        if retry_count > max_retries:
            return PolicyResult(
                decision="deny",
                reason=(
                    f"Retry circuit breaker: {retry_count} retries in last "
                    f"{window_min} minutes (limit: {max_retries}). "
                    f"Investigate output quality of upstream nodes."
                ),
                policy_id=policy.id,
                policy_name=policy.name,
            )
        return None

    # Unknown policy_type — log and skip rather than crash
    logger.warning(
        "Unknown policy_type %r on policy %s — skipping",
        policy.policy_type,
        policy.id,
    )
    return None


# ── Main evaluate function ─────────────────────────────────────────────────────

async def evaluate(
    req: EvaluateRequest,
    db: AsyncSession,
) -> PolicyResult:
    """
    Evaluate all applicable policies for a request and return a decision.

    Called synchronously before every AI provider call.
    Evaluates all applicable policies and returns the most restrictive outcome
    (deny > throttle > allow with suggested downshift > allow).
    Returns allow if no policy matches or all matching policies have effect=warn.
    """
    t_start = time.perf_counter()
    now = datetime.now(timezone.utc)

    # ── Step 1: Check enforcement_state on the app ─────────────────────────────
    app_result = await db.execute(
        select(App).where(App.id == req.app_id, App.is_active == True)
    )
    app = app_result.scalar_one_or_none()

    if app is None:
        return PolicyResult(
            decision="deny",
            reason="App not found or deactivated.",
        )

    if app.enforcement_state == "admin_suspended":
        return PolicyResult(
            decision="deny",
            reason=(
                f"App is suspended by administrator. "
                f"Reason: {app.enforcement_suspended_reason or 'No reason provided.'}"
            ),
        )

    if app.enforcement_state == "budget_suspended":
        return PolicyResult(
            decision="deny",
            reason=(
                f"App has been suspended due to budget cap breach. "
                f"Reason: {app.enforcement_suspended_reason or 'Budget cap exceeded.'}"
            ),
        )

    if app.enforcement_state == "rate_limited":
        return PolicyResult(
            decision="throttle",
            reason=(
                f"App is rate limited. "
                f"{app.enforcement_suspended_reason or 'Rate limit exceeded.'}"
            ),
            retry_after_seconds=60,
        )

    # ── Step 2: Load active policies (app → team → platform order) ─────────────
    policy_q = (
        select(GovernancePolicy)
        .where(GovernancePolicy.is_active == True)
        .where(
            # Policies that apply to this app:
            # - exact app match
            # - team match (app_id null)
            # - platform (both null)
            (GovernancePolicy.app_id == req.app_id)
            | (
                (GovernancePolicy.team_id == req.team_id)
                & GovernancePolicy.app_id.is_(None)
            )
            | (
                GovernancePolicy.team_id.is_(None)
                & GovernancePolicy.app_id.is_(None)
            )
        )
        .order_by(
            # App-scope first, then team, then platform
            # Achieved by mapping scope to a sort key
            # priority within same scope
            GovernancePolicy.priority.asc(),
        )
    )
    policy_result = await db.execute(policy_q)
    policies = policy_result.scalars().all()

    # Sort by scope specificity (app > team > platform) then priority
    def _scope_order(p: GovernancePolicy) -> tuple:
        order = {"app": 0, "team": 1, "platform": 2}
        return (order.get(p.scope, 99), p.priority)

    policies = sorted(policies, key=_scope_order)

    # ── Step 3: Evaluate EVERY applicable policy, then combine ────────────────
    # No policy may short-circuit the others: a degradation ladder that merely
    # downshifts the model (decision="allow") must not skip the budget caps,
    # denylists, rate limits... that come after it. Outcomes are combined as
    #   deny  >  throttle  >  allow-with-downshift  >  allow
    # (most restrictive wins; ties go to the earlier policy in app > team >
    # platform, priority order).
    blockers: list[PolicyResult] = []
    downshifts: list[PolicyResult] = []
    warn_fired = False

    for policy in policies:
        result = await _evaluate_policy(policy, req, db, now)
        if result is None:
            continue  # conditions didn't match / policy did not fire

        if policy.effect == "warn":
            # Warn = fire an alert but don't block. Record it, keep evaluating.
            warn_fired = True
            logger.warning(
                "Policy warn fired",
                extra={
                    "policy_id": policy.id,
                    "policy_name": policy.name,
                    "app_id": req.app_id,
                    "reason": result.reason,
                },
            )
            continue  # warn never changes the outcome

        if result.decision in ("deny", "throttle"):
            blockers.append(result)
        elif result.suggested_model:
            downshifts.append(result)  # allowed, but with a cheaper model

    final_result: PolicyResult = _ALLOW
    if blockers:
        denies = [r for r in blockers if r.decision == "deny"]
        final_result = (denies or blockers)[0]
    elif downshifts:
        def _consumed(r: PolicyResult) -> Decimal:
            if r.spend_limit:
                return (r.spend_at_decision or Decimal("0")) / r.spend_limit
            return Decimal("0")
        final_result = max(downshifts, key=_consumed)  # deepest tier; stable on ties

    # ── Step 4: Record non-allow decisions to policy_decisions ────────────────
    latency_ms = int((time.perf_counter() - t_start) * 1000)

    if final_result.decision != "allow" or warn_fired:
        await _record_decision(
            result=final_result,
            req=req,
            latency_ms=latency_ms,
            db=db,
        )

    return final_result


async def evaluate_policies(
    db: AsyncSession,
    app_id: str,
    team_id: str,
    provider: str,
    model: str,
    environment: str = "production",
    estimated_tokens: int | None = None,
    estimated_cost: Decimal | None = None,
) -> PolicyResult:
    """
    Convenience wrapper for the /api/v1/evaluate endpoint.
    Builds an EvaluateRequest from keyword args and delegates to evaluate().
    """
    req = EvaluateRequest(
        app_id=app_id,
        team_id=team_id,
        provider=provider,
        model=model,
        environment=environment,
        estimated_tokens=estimated_tokens,
        estimated_cost=estimated_cost,
    )
    return await evaluate(req, db)


async def _record_decision(
    result: PolicyResult,
    req: EvaluateRequest,
    latency_ms: int,
    db: AsyncSession,
) -> None:
    """Write a PolicyDecision row. Fire-and-forget from the caller's perspective."""
    try:
        row = PolicyDecision(
            policy_id=result.policy_id,
            app_id=req.app_id,
            team_id=req.team_id,
            decision=result.decision,
            reason=result.reason,
            request_provider=req.provider,
            request_model=req.model,
            request_environment=req.environment,
            request_estimated_tokens=req.estimated_tokens,
            request_estimated_cost=req.estimated_cost,
            spend_at_decision=result.spend_at_decision,
            spend_limit=result.spend_limit,
            evaluation_latency_ms=latency_ms,
        )
        db.add(row)
        # Note: caller is responsible for committing the session.
    except Exception as exc:
        logger.error("Failed to record policy decision", exc_info=exc)
        try:
            await db.rollback()
        except Exception:
            pass  # rollback itself failed — session is likely already invalidated


# ── Real-time spend counter update ─────────────────────────────────────────────

async def update_real_time_spend(
    app_id: str,
    team_id: str,
    total_cost: Decimal,
    call_count: int,
    input_tokens: int,
    output_tokens: int,
    total_duration_ms: int,
    db: AsyncSession,
) -> None:
    """
    Atomically increment real-time spend counters for all relevant period windows.
    Called from the ingest endpoint after every accepted batch.

    Uses PostgreSQL/SQLite upsert with atomic addition — safe for concurrent ingest pods.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from orchestrator.core.config import settings

    now = datetime.now(timezone.utc)
    is_sqlite = settings.is_sqlite
    insert_fn = sqlite_insert if is_sqlite else pg_insert

    for period in ("hourly", "daily", "monthly"):
        wkey, wstart, wend = _window_key(period, now)

        stmt = insert_fn(RealTimeSpend).values(
            id=str(__import__("uuid").uuid4()),
            app_id=app_id,
            team_id=team_id,
            period=period,
            window_key=wkey,
            window_start=wstart,
            window_end=wend,
            total_cost=total_cost,
            call_count=call_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_duration_ms=total_duration_ms,
        )

        if is_sqlite:
            stmt = stmt.on_conflict_do_update(
                index_elements=["app_id", "period", "window_key"],
                set_={
                    "total_cost": RealTimeSpend.total_cost + total_cost,
                    "call_count": RealTimeSpend.call_count + call_count,
                    "input_tokens": RealTimeSpend.input_tokens + input_tokens,
                    "output_tokens": RealTimeSpend.output_tokens + output_tokens,
                    "total_duration_ms": RealTimeSpend.total_duration_ms + total_duration_ms,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        else:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_rts_app_window",
                set_={
                    "total_cost": RealTimeSpend.total_cost + total_cost,
                    "call_count": RealTimeSpend.call_count + call_count,
                    "input_tokens": RealTimeSpend.input_tokens + input_tokens,
                    "output_tokens": RealTimeSpend.output_tokens + output_tokens,
                    "total_duration_ms": RealTimeSpend.total_duration_ms + total_duration_ms,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        await db.execute(stmt)
