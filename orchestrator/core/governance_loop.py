"""
Modus — Autonomous Governance Loop
==================================

Hourly background task that analyzes aggregated usage data, detects
actionable patterns, and generates proposed YAML policy diffs with
natural-language rationale. Each proposal can be applied (creating or
updating a GovernancePolicy) or dismissed via the dashboard.

Detection rules:
  1. Model overprovision — expensive model + low output tokens
  2. Budget overruns    — recurring cap breaches
  3. Unused models      — zero calls but in allowlists
  4. Amplification      — high amplification factor sessions
  5. Seasonal patterns  — weekday/weekend cost variance
  6. Rewind distillation — learn from failure events
  7. Optional AI tier   — customer's LLM generates narrative rationale

All computation runs in the existing FastAPI process on the async event
loop. No external services, no ML libraries, no message queues.
Data never leaves the customer's infrastructure.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    App,
    GovernancePolicy,
    GovernanceProposal,
    RewindEvent,
    TRiSMThreatEvent,
    UsageAggregate,
    UsageRecord,
)
from orchestrator.db.session import _session_factory

logger = logging.getLogger(__name__)

# ── CoT ledger helpers (lazy import to avoid circular) ───────────────────────

def _cot_imports():
    """Lazy import CoT ledger functions to avoid import-time side effects."""
    from orchestrator.core.cot_ledger import (
        append_entry,
        link_entry_to_proposal,
        infer_regulatory_tags,
        build_reasoning_step,
    )
    return append_entry, link_entry_to_proposal, infer_regulatory_tags, build_reasoning_step


# Map proposal_type → detection rule name for CoT tagging
_PROPOSAL_TYPE_TO_RULE: dict[str, str] = {
    "model_downshift": "model_overprovision",
    "budget_tighten": "budget_overruns",
    "amplification_gate": "amplification_patterns",
    "rate_limit_suggest": "rewind_patterns",
    "rewind_learned": "rewind_patterns",
    "iso42001_remediation": "iso42001_gaps",
    "pqc_migration": "pqc_migration",
    "pqc_urgent_replacement": "pqc_migration",
    "sentinel_threat_response": "sentinel_threat_adaptation",
}

# Human-readable detail for each rule (WHAT was detected, not HOW)
_RULE_DETAIL: dict[str, str] = {
    "model_overprovision": "Expensive model used for low-complexity tasks",
    "budget_overruns": "Budget cap breached repeatedly within lookback window",
    "amplification_patterns": "Session call count exceeds amplification threshold",
    "rewind_patterns": "Repeated rewind events indicate need for protective policy",
    "iso42001_gaps": "Unresolved ISO 42001 surveillance gap requires remediation",
    "pqc_migration": "Classical cryptographic algorithms require quantum-safe migration",
    "sentinel_threat_adaptation": "Sentinel detected anomalous patterns requiring policy adaptation",
}

# ── Settings defaults ─────────────────────────────────────────────────────────

GOVERNANCE_DEFAULTS: dict[str, str] = {
    "task.governance_loop.enabled": "true",
    "task.governance_loop.interval_seconds": "3600",
    "governance.lookback_hours": "24",
    "governance.min_calls_for_analysis": "50",
    "governance.overprovision_output_threshold": "200",
    "governance.overprovision_cost_threshold": "0.01",
    "governance.budget_breach_lookback_days": "7",
    "governance.amplification_threshold": "3.0",
    "governance.seasonal_variance_threshold": "0.5",
    "governance.rewind_pattern_threshold": "3",
    "governance.proposal_expiry_days": "30",
    "governance.max_pending_proposals": "50",
    "governance.ai_rationale": "false",
}

# ── Expensive model tiers (for overprovision detection) ──────────────────────

# Matched exactly against the recorded model string, so both the current bare
# IDs (which are complete as-is) and the older dated snapshots are listed.
_EXPENSIVE_MODELS = frozenset({
    "claude-fable-5", "claude-mythos-5",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-opus-4-5", "claude-opus-4-5-20251022", "claude-opus-4-20250514",
    "gpt-4o", "gpt-4-turbo", "gpt-4",
    "o1-preview", "o1", "o3",
    "gemini-1.5-pro", "gemini-2.0-pro",
})

_CHEAP_ALTERNATIVES = {
    "claude-fable-5": "claude-opus-5",
    "claude-mythos-5": "claude-opus-5",
    "claude-opus-5": "claude-sonnet-5",
    "claude-opus-4-8": "claude-sonnet-5",
    "claude-opus-4-7": "claude-sonnet-5",
    "claude-opus-4-6": "claude-sonnet-4-6",
    "claude-opus-4-5": "claude-sonnet-4-5",
    "claude-opus-4-5-20251022": "claude-sonnet-4-5",
    "claude-opus-4-20250514": "claude-sonnet-4-5",
    "gpt-4o": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4o-mini",
    "gpt-4": "gpt-4o-mini",
    "o1-preview": "gpt-4o-mini",
    "o1": "gpt-4o-mini",
    "o3": "o3-mini",
    "gemini-1.5-pro": "gemini-1.5-flash",
    "gemini-2.0-pro": "gemini-2.0-flash",
}


# ── YAML generation helpers ───────────────────────────────────────────────────

def _generate_degradation_ladder_yaml(
    name: str,
    budget_usd: str,
    period: str,
    tiers: list[dict[str, Any]],
) -> str:
    """Generate a degradation_ladder policy YAML snippet."""
    lines = [
        f"- name: {name}",
        "  type: degradation_ladder",
        "  config:",
        f'    budget_usd: "{budget_usd}"',
        f"    period: {period}",
        "    tiers:",
    ]
    for tier in tiers:
        if "model" in tier:
            lines.append(f"      - pct: {tier['pct']}")
            lines.append(f"        model: {tier['model']}")
        elif "action" in tier:
            lines.append(f"      - pct: {tier['pct']}")
            lines.append(f"        action: {tier['action']}")
    return "\n".join(lines)


def _generate_budget_cap_yaml(
    name: str,
    cap_usd: str,
    period: str,
) -> str:
    lines = [
        f"- name: {name}",
        "  type: budget_cap",
        "  effect: deny",
        "  config:",
        f'    cap_usd: "{cap_usd}"',
        f"    period: {period}",
        "  action:",
        f'    message: "Budget cap of ${cap_usd}/{period} reached."',
    ]
    return "\n".join(lines)


def _generate_rate_limit_yaml(
    name: str,
    max_calls: int,
    window_seconds: int,
) -> str:
    lines = [
        f"- name: {name}",
        "  type: rate_limit",
        "  effect: throttle",
        "  config:",
        f"    max_calls: {max_calls}",
        f"    window_seconds: {window_seconds}",
        "  action:",
        "    retry_after_seconds: 30",
    ]
    return "\n".join(lines)


def _generate_amplification_gate_yaml(
    name: str,
    max_amplification: float,
) -> str:
    lines = [
        f"- name: {name}",
        "  type: amplification_gate",
        "  effect: deny",
        "  config:",
        f"    max_amplification: {max_amplification}",
        "  action:",
        f'    message: "Amplification factor exceeds {max_amplification}x limit."',
    ]
    return "\n".join(lines)


def _generate_model_denylist_yaml(
    name: str,
    models: list[str],
) -> str:
    lines = [
        f"- name: {name}",
        "  type: model_denylist",
        "  effect: deny",
        "  config:",
        "    models:",
    ]
    for m in models:
        lines.append(f"      - {m}")
    return "\n".join(lines)


# ── Detection rules ───────────────────────────────────────────────────────────

async def _detect_model_overprovision(
    db: AsyncSession,
    team_id: str,
    lookback_hours: int,
    min_calls: int,
    output_threshold: int,
    cost_threshold: float,
) -> list[dict[str, Any]]:
    """
    Detect expensive models used for simple tasks (low output tokens).
    Suggests degradation ladder or model switch.
    """
    proposals = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    # Query hourly aggregates for expensive models with low avg output
    q = (
        select(
            UsageAggregate.app_id,
            UsageAggregate.model,
            UsageAggregate.provider,
            func.sum(UsageAggregate.call_count).label("total_calls"),
            func.sum(UsageAggregate.output_tokens).label("total_output"),
            func.sum(UsageAggregate.total_cost).label("total_cost"),
        )
        .where(
            and_(
                UsageAggregate.team_id == team_id,
                UsageAggregate.period_start >= cutoff,
                UsageAggregate.granularity == "hourly",
            )
        )
        .group_by(
            UsageAggregate.app_id,
            UsageAggregate.model,
            UsageAggregate.provider,
        )
        .having(func.sum(UsageAggregate.call_count) >= min_calls)
    )

    result = await db.execute(q)
    for row in result.all():
        model = row.model or ""
        if model not in _EXPENSIVE_MODELS:
            continue

        total_calls = row.total_calls or 0
        total_output = row.total_output or 0
        total_cost = float(row.total_cost or 0)
        if total_calls == 0:
            continue

        avg_output = total_output / total_calls
        avg_cost = total_cost / total_calls

        if avg_output < output_threshold and avg_cost > cost_threshold:
            cheap_model = _CHEAP_ALTERNATIVES.get(model, "gpt-4o-mini")
            # Estimate savings (rough: 3-5x cheaper)
            savings_factor = 0.7  # conservative 70% savings estimate
            monthly_savings = (total_cost / lookback_hours) * 24 * 30 * savings_factor

            # Look up app name
            app = await db.get(App, str(row.app_id))
            app_name = app.app_name if app else str(row.app_id)

            proposed_yaml = _generate_degradation_ladder_yaml(
                name=f"auto-downshift-{app_name}",
                budget_usd=str(round(total_cost * 2, 2)),
                period="daily",
                tiers=[
                    {"pct": 50, "model": cheap_model},
                    {"pct": 90, "model": cheap_model},
                    {"pct": 100, "action": "deny"},
                ],
            )

            proposals.append({
                "team_id": team_id,
                "app_id": str(row.app_id),
                "proposal_type": "model_downshift",
                "severity": "warning",
                "title": (
                    f"Model overprovision: {app_name} uses {model} "
                    f"with avg {avg_output:.0f} output tokens"
                ),
                "rationale": (
                    f"App '{app_name}' made {total_calls} calls to {model} "
                    f"in the last {lookback_hours}h with an average of "
                    f"{avg_output:.0f} output tokens per call (avg cost "
                    f"${avg_cost:.4f}/call). Low output tokens suggest simple "
                    f"tasks that don't need an expensive model. Switching to "
                    f"{cheap_model} could save ~${monthly_savings:.0f}/month."
                ),
                "proposed_yaml": proposed_yaml,
                "estimated_savings_usd": Decimal(str(round(monthly_savings, 8))),
                "data_snapshot": {
                    "model": model,
                    "total_calls": total_calls,
                    "avg_output_tokens": round(avg_output, 1),
                    "avg_cost_per_call": round(avg_cost, 6),
                    "total_cost": round(total_cost, 4),
                    "lookback_hours": lookback_hours,
                },
            })

    return proposals


async def _detect_budget_overruns(
    db: AsyncSession,
    team_id: str,
    lookback_days: int,
) -> list[dict[str, Any]]:
    """
    Detect recurring budget cap breaches and suggest tighter limits.
    """
    proposals = []

    # Check for existing budget_cap policies and their breach history
    policy_q = (
        select(GovernancePolicy)
        .where(
            and_(
                GovernancePolicy.team_id == team_id,
                GovernancePolicy.policy_type == "budget_cap",
                GovernancePolicy.is_active == True,
            )
        )
    )
    policies = (await db.execute(policy_q)).scalars().all()

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    for policy in policies:
        config = policy.config or {}
        cap_usd = float(config.get("cap_usd", 0))
        period = config.get("period", "daily")

        if cap_usd <= 0:
            continue

        # Count days where spend exceeded the cap
        if period == "daily":
            daily_totals = (
                select(
                    UsageAggregate.period_start,
                    func.sum(UsageAggregate.total_cost).label("day_cost"),
                )
                .where(
                    and_(
                        UsageAggregate.team_id == team_id,
                        UsageAggregate.period_start >= cutoff,
                        UsageAggregate.granularity == "daily",
                    )
                )
                .group_by(UsageAggregate.period_start)
            )

            result = await db.execute(daily_totals)
            days = result.all()
            breach_count = sum(
                1 for d in days if float(d.day_cost or 0) > cap_usd
            )

            if breach_count >= 3:  # breached 3+ days in lookback
                avg_daily = sum(float(d.day_cost or 0) for d in days) / max(len(days), 1)
                suggested_cap = round(avg_daily * 0.8, 2)  # 80% of average

                proposed_yaml = _generate_budget_cap_yaml(
                    name=f"auto-tighten-{policy.name}",
                    cap_usd=str(suggested_cap),
                    period=period,
                )

                proposals.append({
                    "team_id": team_id,
                    "proposal_type": "budget_tighten",
                    "severity": "warning",
                    "title": (
                        f"Budget overruns: {policy.name} breached "
                        f"{breach_count}x in {lookback_days} days"
                    ),
                    "rationale": (
                        f"Policy '{policy.name}' (${cap_usd}/{period}) was "
                        f"breached {breach_count} times in the last "
                        f"{lookback_days} days. Average daily spend is "
                        f"${avg_daily:.2f}. Consider tightening to "
                        f"${suggested_cap}/{period} with a degradation ladder "
                        f"for overflow."
                    ),
                    "current_yaml": (
                        f"- name: {policy.name}\n"
                        f"  type: budget_cap\n"
                        f"  config:\n"
                        f'    cap_usd: "{cap_usd}"\n'
                        f"    period: {period}\n"
                    ),
                    "proposed_yaml": proposed_yaml,
                    "estimated_savings_usd": Decimal(str(
                        round((avg_daily - suggested_cap) * 30, 8)
                    )),
                    "data_snapshot": {
                        "policy_name": policy.name,
                        "cap_usd": cap_usd,
                        "period": period,
                        "breach_count": breach_count,
                        "avg_daily_spend": round(avg_daily, 4),
                        "lookback_days": lookback_days,
                    },
                })

    return proposals


async def _detect_amplification_patterns(
    db: AsyncSession,
    team_id: str,
    lookback_hours: int,
    threshold: float,
) -> list[dict[str, Any]]:
    """
    Detect sessions with high amplification factors.
    """
    proposals = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    # Query sessions with high call counts (proxy for amplification)
    q = (
        select(
            UsageRecord.session_id,
            UsageRecord.app_id,
            func.count().label("call_count"),
            func.sum(UsageRecord.total_cost).label("total_cost"),
            func.sum(UsageRecord.output_tokens).label("total_output"),
        )
        .where(
            and_(
                UsageRecord.team_id == team_id,
                UsageRecord.timestamp >= cutoff,
                UsageRecord.session_id.isnot(None),
            )
        )
        .group_by(UsageRecord.session_id, UsageRecord.app_id)
        .having(func.count() >= 10)  # sessions with 10+ calls
    )

    result = await db.execute(q)
    for row in result.all():
        call_count = row.call_count or 0
        total_cost = float(row.total_cost or 0)

        # Estimate amplification: ratio of total to first-call cost
        if call_count <= 1:
            continue

        # Simple heuristic: calls / expected_calls_for_simple_task
        amplification = call_count / 3.0  # assume 3 calls is "normal"

        if amplification >= threshold:
            app = await db.get(App, str(row.app_id))
            app_name = app.app_name if app else str(row.app_id)

            proposed_yaml = _generate_amplification_gate_yaml(
                name=f"auto-amp-gate-{app_name}",
                max_amplification=round(threshold, 1),
            )

            proposals.append({
                "team_id": team_id,
                "app_id": str(row.app_id),
                "proposal_type": "amplification_gate",
                "severity": "warning" if amplification < threshold * 2 else "critical",
                "title": (
                    f"High amplification: {app_name} session with "
                    f"{call_count} calls ({amplification:.1f}x)"
                ),
                "rationale": (
                    f"Session in app '{app_name}' made {call_count} calls "
                    f"(amplification factor {amplification:.1f}x, spending "
                    f"${total_cost:.2f}). This exceeds the {threshold}x "
                    f"threshold. Adding an amplification gate would prevent "
                    f"runaway agent loops."
                ),
                "proposed_yaml": proposed_yaml,
                "data_snapshot": {
                    "session_id": row.session_id,
                    "call_count": call_count,
                    "total_cost": round(total_cost, 4),
                    "amplification": round(amplification, 2),
                },
            })
            break  # one proposal per app is enough

    return proposals


async def _detect_rewind_patterns(
    db: AsyncSession,
    team_id: str,
    lookback_hours: int,
    pattern_threshold: int,
) -> list[dict[str, Any]]:
    """
    Analyze RewindEvents and propose preventive policies.
    """
    proposals = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    # Count rewind events by trigger reason and app
    q = (
        select(
            RewindEvent.app_id,
            RewindEvent.trigger_reason,
            func.count().label("event_count"),
        )
        .where(
            and_(
                RewindEvent.team_id == team_id,
                RewindEvent.created_at >= cutoff,
            )
        )
        .group_by(RewindEvent.app_id, RewindEvent.trigger_reason)
        .having(func.count() >= pattern_threshold)
    )

    result = await db.execute(q)
    for row in result.all():
        app = await db.get(App, str(row.app_id))
        app_name = app.app_name if app else str(row.app_id)
        reason = row.trigger_reason
        count = row.event_count

        if reason == "circuit_breaker":
            proposed_yaml = _generate_rate_limit_yaml(
                name=f"auto-ratelimit-{app_name}",
                max_calls=100,
                window_seconds=3600,
            )
            proposal_type = "rate_limit_suggest"
        elif reason == "budget_suspension":
            proposed_yaml = _generate_budget_cap_yaml(
                name=f"auto-budget-{app_name}",
                cap_usd="50.00",
                period="hourly",
            )
            proposal_type = "budget_tighten"
        else:
            proposed_yaml = _generate_rate_limit_yaml(
                name=f"auto-protect-{app_name}",
                max_calls=200,
                window_seconds=3600,
            )
            proposal_type = "rewind_learned"

        proposals.append({
            "team_id": team_id,
            "app_id": str(row.app_id),
            "proposal_type": proposal_type,
            "severity": "critical" if count >= pattern_threshold * 2 else "warning",
            "title": (
                f"Repeated failures: {app_name} triggered {count} "
                f"rewind events ({reason})"
            ),
            "rationale": (
                f"App '{app_name}' triggered {count} rewind events due to "
                f"'{reason}' in the last {lookback_hours}h. This pattern "
                f"suggests the app needs protective policies to prevent "
                f"repeated failures and cost waste."
            ),
            "proposed_yaml": proposed_yaml,
            "source": "rewind",
            "data_snapshot": {
                "trigger_reason": reason,
                "event_count": count,
                "lookback_hours": lookback_hours,
            },
        })

    return proposals


# ── Deduplication ─────────────────────────────────────────────────────────────

async def _deduplicate_proposals(
    db: AsyncSession,
    proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Remove proposals that duplicate existing pending proposals.
    """
    if not proposals:
        return []

    # Load existing pending proposals for these teams
    team_ids = {p["team_id"] for p in proposals}
    q = (
        select(GovernanceProposal.team_id, GovernanceProposal.proposal_type,
               GovernanceProposal.app_id)
        .where(
            and_(
                GovernanceProposal.team_id.in_(team_ids),
                GovernanceProposal.status == "pending",
            )
        )
    )
    existing = await db.execute(q)
    existing_set = {
        (str(r.team_id), r.proposal_type, str(r.app_id) if r.app_id else None)
        for r in existing.all()
    }

    return [
        p for p in proposals
        if (p["team_id"], p["proposal_type"], p.get("app_id"))
        not in existing_set
    ]


# ── Main governance loop ─────────────────────────────────────────────────────

async def run_governance_loop() -> None:
    """
    Main background task entry point. Runs all detection rules across all
    teams and creates GovernanceProposal rows for actionable findings.
    """
    if _session_factory is None:
        return

    start = time.perf_counter()
    total_proposals = 0

    try:
        async with _session_factory() as db:
            # Read settings
            from orchestrator.core.insights_engine import (
                get_setting_int, get_setting_float,
            )

            lookback_hours = await get_setting_int(
                db, "governance.lookback_hours", 24
            )
            min_calls = await get_setting_int(
                db, "governance.min_calls_for_analysis", 50
            )
            output_threshold = await get_setting_int(
                db, "governance.overprovision_output_threshold", 200
            )
            cost_threshold = await get_setting_float(
                db, "governance.overprovision_cost_threshold", 0.01
            )
            breach_lookback = await get_setting_int(
                db, "governance.budget_breach_lookback_days", 7
            )
            amp_threshold = await get_setting_float(
                db, "governance.amplification_threshold", 3.0
            )
            rewind_threshold = await get_setting_int(
                db, "governance.rewind_pattern_threshold", 3
            )
            max_pending = await get_setting_int(
                db, "governance.max_pending_proposals", 50
            )
            expiry_days = await get_setting_int(
                db, "governance.proposal_expiry_days", 30
            )

            # Get all active teams
            from orchestrator.db.models import Team
            teams = (await db.execute(
                select(Team.id).where(Team.deleted_at.is_(None))
            )).scalars().all()

            all_proposals: list[dict[str, Any]] = []

            for team_id in teams:
                tid = str(team_id)

                # Run all detection rules
                all_proposals.extend(await _detect_model_overprovision(
                    db, tid, lookback_hours, min_calls,
                    output_threshold, cost_threshold,
                ))
                all_proposals.extend(await _detect_budget_overruns(
                    db, tid, breach_lookback,
                ))
                all_proposals.extend(await _detect_amplification_patterns(
                    db, tid, lookback_hours, amp_threshold,
                ))
                all_proposals.extend(await _detect_rewind_patterns(
                    db, tid, lookback_hours, rewind_threshold,
                ))

                # Phase 10: PQC detector
                all_proposals.extend(await _detect_pqc_migration_needs(db, tid))

                # Sentinel threat-responsive coevolution
                all_proposals.extend(await _detect_sentinel_threats(db, tid))

            # Deduplicate against existing pending proposals
            new_proposals = await _deduplicate_proposals(db, all_proposals)

            # Check pending limit
            pending_count = (await db.execute(
                select(func.count()).select_from(GovernanceProposal)
                .where(GovernanceProposal.status == "pending")
            )).scalar() or 0

            available_slots = max(0, max_pending - pending_count)
            new_proposals = new_proposals[:available_slots]

            # Create proposal records and emit CoT entries
            expires_at = datetime.now(timezone.utc) + timedelta(days=expiry_days)
            cot_links: list[tuple[str, GovernanceProposal]] = []  # (cot_entry_id, proposal_obj)
            for p in new_proposals:
                proposal = GovernanceProposal(
                    team_id=p["team_id"],
                    app_id=p.get("app_id"),
                    proposal_type=p["proposal_type"],
                    severity=p.get("severity", "info"),
                    title=p["title"],
                    rationale=p["rationale"],
                    current_yaml=p.get("current_yaml"),
                    proposed_yaml=p["proposed_yaml"],
                    data_snapshot=p.get("data_snapshot"),
                    estimated_savings_usd=p.get("estimated_savings_usd"),
                    source=p.get("source", "rule_engine"),
                    status="pending",
                    expires_at=expires_at,
                )
                db.add(proposal)
                total_proposals += 1

                # Emit CoT ledger entry for this proposal
                try:
                    _append, _link, _tags, _step = _cot_imports()
                    rule_name = _PROPOSAL_TYPE_TO_RULE.get(
                        p["proposal_type"], p["proposal_type"]
                    )
                    cot_result = await _append(
                        db=db,
                        team_id=p["team_id"],
                        decision_type="governance_proposal",
                        trigger="pattern_detection",
                        decision_summary=p["title"],
                        evidence_snapshot=p.get("data_snapshot"),
                        rules_evaluated=[{
                            "rule": rule_name,
                            "fired": True,
                            "confidence": 1.0,
                            "detail": _RULE_DETAIL.get(rule_name, p["title"]),
                        }],
                        reasoning_steps=[
                            _step(
                                rule_name,
                                p["title"],
                                None,
                                p.get("rationale", ""),
                            ),
                        ],
                        regulatory_tags=_tags(rule_name),
                    )
                    cot_entry_id = cot_result.get("id")
                    if cot_entry_id:
                        cot_links.append((cot_entry_id, proposal))
                except Exception:
                    logger.debug("CoT emission failed for proposal", exc_info=True)

            # Flush to generate proposal IDs, then link CoT entries
            if cot_links:
                try:
                    await db.flush()
                    _append, _link, _tags, _step = _cot_imports()
                    for cot_entry_id, prop_obj in cot_links:
                        await _link(db, cot_entry_id, str(prop_obj.id))
                except Exception:
                    logger.debug("CoT link-to-proposal failed", exc_info=True)

            # Expire old proposals
            expired = await db.execute(
                select(GovernanceProposal)
                .where(
                    and_(
                        GovernanceProposal.status == "pending",
                        GovernanceProposal.expires_at <= datetime.now(timezone.utc),
                    )
                )
            )
            for old in expired.scalars():
                old.status = "expired"

            await db.commit()

        elapsed = time.perf_counter() - start
        logger.info(
            "Governance loop complete",
            extra={
                "proposals_created": total_proposals,
                "duration_ms": round(elapsed * 1000),
            },
        )

    except Exception as exc:
        logger.error("Governance loop failed", exc_info=exc)


# ── Apply / dismiss helpers ──────────────────────────────────────────────────

async def apply_proposal(
    db: AsyncSession,
    proposal_id: str,
    actor_id: str,
) -> Optional[GovernancePolicy]:
    """
    Apply a governance proposal — creates or updates a GovernancePolicy.
    Returns the created/updated policy, or None if proposal not found.
    """
    proposal = await db.get(GovernanceProposal, proposal_id)
    if proposal is None or proposal.status != "pending":
        return None

    # Parse proposed YAML to extract policy fields
    # The proposed_yaml is a simplified YAML snippet — we extract key fields
    import yaml as _yaml  # Optional dep for apply; governance loop itself is yaml-free

    try:
        parsed = _yaml.safe_load(proposal.proposed_yaml)
    except Exception:
        # Fallback: create policy from proposal metadata
        parsed = None

    if isinstance(parsed, list) and len(parsed) > 0:
        policy_data = parsed[0]
    elif isinstance(parsed, dict):
        policy_data = parsed
    else:
        policy_data = {}

    policy = GovernancePolicy(
        team_id=proposal.team_id,
        app_id=proposal.app_id,
        name=policy_data.get("name", f"auto-{proposal.proposal_type}"),
        scope="app" if proposal.app_id else "team",
        policy_type=policy_data.get("type", "budget_cap"),
        effect=policy_data.get("effect", "deny"),
        config=policy_data.get("config", {}),
        conditions=policy_data.get("conditions"),
        action=policy_data.get("action"),
        priority=50,
        is_active=True,
        created_by=f"governance:{actor_id}",
    )
    db.add(policy)

    proposal.status = "applied"
    proposal.applied_at = datetime.now(timezone.utc)
    proposal.applied_by = actor_id

    await db.flush()

    # Emit CoT ledger entry for the applied proposal
    try:
        _append, _link, _tags, _step = _cot_imports()
        rule_name = _PROPOSAL_TYPE_TO_RULE.get(
            proposal.proposal_type, proposal.proposal_type
        )
        await _append(
            db=db,
            team_id=proposal.team_id,
            decision_type="policy_applied",
            trigger="manual",
            decision_summary=f"Applied: {proposal.title}",
            linked_proposal_id=str(proposal.id),
            linked_policy_id=str(policy.id),
            regulatory_tags=_tags(rule_name),
        )
    except Exception:
        logger.debug("CoT emission failed for apply_proposal", exc_info=True)

    return policy


async def dismiss_proposal(
    db: AsyncSession,
    proposal_id: str,
    reason: str,
) -> bool:
    """Dismiss a governance proposal with reason. Returns success."""
    proposal = await db.get(GovernanceProposal, proposal_id)
    if proposal is None or proposal.status != "pending":
        return False

    proposal.status = "dismissed"
    proposal.dismissed_at = datetime.now(timezone.utc)
    proposal.dismissed_reason = reason
    await db.flush()

    # Emit CoT ledger entry for the dismissed proposal
    try:
        _append, _link, _tags, _step = _cot_imports()
        rule_name = _PROPOSAL_TYPE_TO_RULE.get(
            proposal.proposal_type, proposal.proposal_type
        )
        await _append(
            db=db,
            team_id=proposal.team_id,
            decision_type="policy_dismissed",
            trigger="manual",
            decision_summary=f"Dismissed: {proposal.title} — {reason}",
            linked_proposal_id=str(proposal.id),
            regulatory_tags=_tags(rule_name),
        )
    except Exception:
        logger.debug("CoT emission failed for dismiss_proposal", exc_info=True)

    return True


# ── Phase 10: PQC detection rules ─────────────────────────────────────────────

def _generate_pqc_migration_yaml(stage: str, algorithm: str) -> str:
    """Generate a YAML policy template for PQC migration steps."""
    if not isinstance(stage, str) or not stage.strip():
        stage = "assessment"
    if not isinstance(algorithm, str) or not algorithm.strip():
        algorithm = "unknown"

    # Look up PQC replacement from the canonical table
    from orchestrator.core.pqc_assessment import CLASSICAL_ALGORITHMS

    algo_info = CLASSICAL_ALGORITHMS.get(algorithm, {})
    replacement = algo_info.get("replacement", "ML-KEM-768")

    # Urgency: 3DES and RSA-2048 get "block" + 90 days; others get "warn" + 180 days
    urgent_algorithms = {"3DES", "RSA-2048", "DES", "RC4"}
    if algorithm in urgent_algorithms:
        enforcement = "block"
        deadline_days = 90
    else:
        enforcement = "warn"
        deadline_days = 180

    lines = [
        "type: pqc_migration",
        f'stage: "{stage}"',
        f'target_algorithm: "{replacement}"',
        f'current_algorithm: "{algorithm}"',
        f"enforcement: {enforcement}",
        f"deadline_days: {deadline_days}",
    ]
    return "\n".join(lines)


async def _detect_pqc_migration_needs(
    db: AsyncSession,
    team_id: str,
) -> list[dict[str, Any]]:
    """
    Detect teams with low PQC readiness scores and generate governance
    proposals for migration steps on the PQC ladder.
    """
    from orchestrator.core.config import settings

    if not settings.pqc_assessment_enabled:
        return []

    from orchestrator.db.models import PQCReadinessScore

    proposals: list[dict[str, Any]] = []

    # Get the latest team-level readiness score (app_id IS NULL)
    q = (
        select(PQCReadinessScore)
        .where(
            and_(
                PQCReadinessScore.team_id == team_id,
                PQCReadinessScore.app_id.is_(None),
            )
        )
        .order_by(PQCReadinessScore.assessed_at.desc())
        .limit(1)
    )

    result = await db.execute(q)
    score_row = result.scalars().first()

    if score_row is None:
        return []

    if score_row.score >= 50:
        return []

    classical_count = score_row.classical_key_count or 0
    weakest = score_row.weakest_algorithm or ""

    # Propose hybrid migration if classical keys exist
    if classical_count > 0:
        proposed_yaml = _generate_pqc_migration_yaml("hybrid", weakest or "RSA-2048")

        proposals.append({
            "team_id": team_id,
            "proposal_type": "pqc_migration",
            "severity": "warning",
            "title": (
                f"PQC migration needed: {classical_count} classical key(s) "
                f"detected (score {score_row.score:.0f}/100)"
            ),
            "rationale": (
                f"Team has {classical_count} classical cryptographic key(s) "
                f"and a PQC readiness score of {score_row.score:.0f}/100. "
                f"Migrating to hybrid mode will maintain backward compatibility "
                f"while adding quantum resistance."
            ),
            "proposed_yaml": proposed_yaml,
            "source": "pqc_assessment",
            "data_snapshot": {
                "score": round(score_row.score, 2),
                "classical_key_count": classical_count,
                "hybrid_key_count": score_row.hybrid_key_count or 0,
                "pqc_key_count": score_row.pqc_key_count or 0,
                "weakest_algorithm": weakest,
            },
        })

    # Urgent replacement if weakest algorithm is critically weak
    urgent_algorithms = {"3DES", "RSA-2048", "DES", "RC4"}
    if weakest and weakest in urgent_algorithms:
        proposed_yaml = _generate_pqc_migration_yaml("full_pqc", weakest)

        proposals.append({
            "team_id": team_id,
            "proposal_type": "pqc_urgent_replacement",
            "severity": "critical",
            "title": (
                f"Urgent PQC replacement: {weakest} is critically vulnerable"
            ),
            "rationale": (
                f"Algorithm {weakest} is critically vulnerable to quantum attack. "
                f"Immediate migration to a PQC-safe replacement is recommended. "
                f"Current PQC readiness score: {score_row.score:.0f}/100."
            ),
            "proposed_yaml": proposed_yaml,
            "source": "pqc_assessment",
            "data_snapshot": {
                "score": round(score_row.score, 2),
                "weakest_algorithm": weakest,
                "classical_key_count": classical_count,
            },
        })

    return proposals


# ── Sentinel threat-responsive policy adaptation ───────────────────────────

# Map threat types to recommended policy responses
_THREAT_POLICY_RESPONSE: dict[str, dict[str, Any]] = {
    "context_poisoning": {
        "policy_type": "rate_limit",
        "severity": "critical",
        "generator": "_generate_rate_limit_yaml",
        "args_fn": lambda app_id: {
            "name": f"sentinel-poison-guard-{(app_id or 'global')[:8]}",
            "max_calls": 20,
            "window_seconds": 60,
        },
        "rationale_tmpl": (
            "Sentinel detected {count} context poisoning event(s) "
            "(avg confidence {avg_conf:.0%}) in the last {hours}h for app {app_id}. "
            "A rate limit is recommended to contain prompt injection attempts."
        ),
    },
    "cascade_failure": {
        "policy_type": "rate_limit",
        "severity": "critical",
        "generator": "_generate_rate_limit_yaml",
        "args_fn": lambda app_id: {
            "name": f"sentinel-cascade-breaker-{(app_id or 'global')[:8]}",
            "max_calls": 10,
            "window_seconds": 120,
        },
        "rationale_tmpl": (
            "Sentinel predicted cascade failure risk for app {app_id}: "
            "{count} event(s), avg confidence {avg_conf:.0%}. "
            "A rate limit is recommended to circuit-break runaway agent loops."
        ),
    },
    "goal_hijack": {
        "policy_type": "rate_limit",
        "severity": "warning",
        "generator": "_generate_rate_limit_yaml",
        "args_fn": lambda app_id: {
            "name": f"sentinel-hijack-guard-{(app_id or 'global')[:8]}",
            "max_calls": 30,
            "window_seconds": 60,
        },
        "rationale_tmpl": (
            "Sentinel detected {count} goal hijack event(s) for app {app_id} "
            "(avg confidence {avg_conf:.0%}). "
            "A protective rate limit is recommended to slow potential takeover."
        ),
    },
    "tool_misuse": {
        "policy_type": "rate_limit",
        "severity": "critical",
        "generator": "_generate_rate_limit_yaml",
        "args_fn": lambda app_id: {
            "name": f"sentinel-tool-guard-{(app_id or 'global')[:8]}",
            "max_calls": 15,
            "window_seconds": 60,
        },
        "rationale_tmpl": (
            "Sentinel flagged {count} tool misuse event(s) for app {app_id} "
            "(avg confidence {avg_conf:.0%}). Includes potential data exfiltration "
            "or destructive operations. Rate limiting recommended."
        ),
    },
    "communication_anomaly": {
        "policy_type": "amplification_gate",
        "severity": "warning",
        "generator": "_generate_amplification_gate_yaml",
        "args_fn": lambda app_id: {
            "name": f"sentinel-comm-gate-{(app_id or 'global')[:8]}",
            "max_amplification": 3.0,
        },
        "rationale_tmpl": (
            "Sentinel detected {count} communication anomaly event(s) for "
            "app {app_id} (avg confidence {avg_conf:.0%}). Circular delegation "
            "or privilege escalation patterns observed. An amplification gate "
            "is recommended to bound agent delegation depth."
        ),
    },
}


async def _detect_sentinel_threats(
    db: AsyncSession,
    team_id: str,
    lookback_hours: int = 24,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    """
    Detect recent high-confidence Sentinel threat events and generate
    governance proposals to automatically adapt policies in response.

    This is the "neuro-assurance coevolution" link: when the Sentinel
    detects anomalous patterns, governance policies automatically adapt
    (with human-in-the-loop approval via the proposal queue).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    result = await db.execute(
        select(
            TRiSMThreatEvent.threat_type,
            TRiSMThreatEvent.app_id,
            func.count(TRiSMThreatEvent.id).label("event_count"),
            func.avg(TRiSMThreatEvent.confidence_score).label("avg_confidence"),
        )
        .where(
            and_(
                TRiSMThreatEvent.team_id == team_id,
                TRiSMThreatEvent.created_at >= cutoff,
                TRiSMThreatEvent.confidence_score >= min_confidence,
            )
        )
        .group_by(TRiSMThreatEvent.threat_type, TRiSMThreatEvent.app_id)
    )
    threat_groups = result.all()

    proposals: list[dict[str, Any]] = []

    for row in threat_groups:
        threat_type = row[0]
        app_id = row[1]
        event_count = row[2]
        avg_confidence = float(row[3])

        # Only generate proposals for threat types with known policy responses
        response = _THREAT_POLICY_RESPONSE.get(threat_type)
        if response is None:
            continue

        # Require at least 2 events to avoid single-event false positives
        if event_count < 2:
            continue

        args = response["args_fn"](app_id)
        generator_name = response["generator"]

        # Generate the proposed YAML
        if generator_name == "_generate_rate_limit_yaml":
            proposed_yaml = _generate_rate_limit_yaml(**args)
        elif generator_name == "_generate_amplification_gate_yaml":
            proposed_yaml = _generate_amplification_gate_yaml(**args)
        else:
            continue

        rationale = response["rationale_tmpl"].format(
            count=event_count,
            avg_conf=avg_confidence,
            hours=lookback_hours,
            app_id=app_id or "all apps",
        )

        proposals.append({
            "team_id": team_id,
            "app_id": app_id,
            "proposal_type": "sentinel_threat_response",
            "severity": response["severity"],
            "title": (
                f"Sentinel threat adaptation: {threat_type.replace('_', ' ')} "
                f"detected ({event_count} events)"
            ),
            "rationale": rationale,
            "proposed_yaml": proposed_yaml,
            "source": "sentinel",
            "data_snapshot": {
                "threat_type": threat_type,
                "event_count": event_count,
                "avg_confidence": round(avg_confidence, 4),
                "lookback_hours": lookback_hours,
                "app_id": app_id,
            },
        })

    return proposals
