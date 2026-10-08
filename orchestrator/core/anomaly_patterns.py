"""
Modus — Pattern Anomaly Detection
======================================
Lightweight anomaly detection that surfaces enforcement patterns as
actionable signals for the governance loop.

Detects recurring patterns in enforcement data and generates
anomaly signals that feed into the CoT Ledger as triggers for
governance decisions.

Runs as part of the hourly governance loop — not a separate task.
Pure Python, stdlib only, no ML dependencies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── Anomaly signal structure ──────────────────────────────────────────────────

def _signal(
    signal_type: str,
    title: str,
    rationale: str,
    severity: str,
    evidence: dict,
    regulatory_tags: list | None = None,
) -> dict:
    """Build a standardized anomaly signal dict."""
    return {
        "signal_type": signal_type,
        "title": title,
        "rationale": rationale,
        "severity": severity,
        "evidence": evidence,
        "regulatory_tags": regulatory_tags or [],
    }


# ── Main entry point ──────────────────────────────────────────────────────────

async def detect_anomaly_patterns(
    db: AsyncSession,
    team_id: str,
    lookback_hours: int = 168,  # 7 days
) -> list[dict]:
    """
    Run all anomaly detectors for a team.
    Returns a list of anomaly signal dicts.
    Called by the governance loop after detection rules.
    """
    signals = []
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    try:
        signals.extend(await _detect_threshold_ceiling(db, team_id, since))
    except Exception as e:
        logger.warning("Anomaly detector threshold_ceiling failed: %s", e)

    try:
        signals.extend(await _detect_model_trigger_patterns(db, team_id, since))
    except Exception as e:
        logger.warning("Anomaly detector model_trigger failed: %s", e)

    try:
        signals.extend(await _detect_team_spend_anomaly(db, team_id, since))
    except Exception as e:
        logger.warning("Anomaly detector team_spend failed: %s", e)

    try:
        signals.extend(await _detect_enforcement_hotspots(db, team_id, since))
    except Exception as e:
        logger.warning("Anomaly detector enforcement_hotspots failed: %s", e)

    if signals:
        logger.info("Detected %d anomaly patterns for team %s", len(signals), team_id[:8])

    return signals


# ── Detector 1: Threshold Ceiling Hits ────────────────────────────────────────

async def _detect_threshold_ceiling(
    db: AsyncSession,
    team_id: str,
    since: datetime,
) -> list[dict]:
    """
    Detect thresholds that are repeatedly hitting their ceiling.
    Signal: "This threshold keeps getting breached — consider adjusting."
    """
    from orchestrator.db.models import Alert, Threshold

    # Count alerts per threshold in the lookback window
    result = await db.execute(
        select(
            Alert.threshold_id,
            func.count(Alert.id).label("breach_count"),
        )
        .where(
            Alert.team_id == team_id,
            Alert.fired_at >= since,
        )
        .group_by(Alert.threshold_id)
        .having(func.count(Alert.id) >= 5)  # 5+ breaches = pattern
    )
    rows = result.all()

    signals = []
    for row in rows:
        # Look up threshold details
        threshold = await db.get(Threshold, row.threshold_id) if row.threshold_id else None
        threshold_name = threshold.name if threshold else "Unknown threshold"

        signals.append(_signal(
            signal_type="threshold_ceiling",
            title=f"Threshold repeatedly breached: {threshold_name}",
            rationale=(
                f"The threshold '{threshold_name}' has been breached {row.breach_count} times "
                f"in the past {(datetime.now(timezone.utc) - since).days} days. "
                f"This pattern suggests the threshold value needs adjustment or the "
                f"underlying behavior driving the breaches needs investigation."
            ),
            severity="warning" if row.breach_count < 10 else "critical",
            evidence={
                "threshold_id": row.threshold_id,
                "threshold_name": threshold_name,
                "breach_count": row.breach_count,
                "lookback_days": (datetime.now(timezone.utc) - since).days,
            },
            regulatory_tags=["nist_ai_rmf_govern_1"],
        ))

    return signals


# ── Detector 2: Model Trigger Patterns ────────────────────────────────────────

async def _detect_model_trigger_patterns(
    db: AsyncSession,
    team_id: str,
    since: datetime,
) -> list[dict]:
    """
    Detect models that keep triggering the same policy.
    Signal: "This model keeps being blocked/throttled — teams may need guidance."
    """
    from orchestrator.db.models import PolicyDecision

    result = await db.execute(
        select(
            PolicyDecision.request_model,
            PolicyDecision.decision,
            PolicyDecision.policy_id,
            func.count(PolicyDecision.id).label("trigger_count"),
        )
        .where(
            PolicyDecision.team_id == team_id,
            PolicyDecision.decided_at >= since,
            PolicyDecision.decision.in_(["deny", "throttle", "degrade"]),
        )
        .group_by(PolicyDecision.request_model, PolicyDecision.decision, PolicyDecision.policy_id)
        .having(func.count(PolicyDecision.id) >= 10)  # 10+ triggers = pattern
    )
    rows = result.all()

    signals = []
    for row in rows:
        if not row.request_model:
            continue
        model_name = row.request_model
        policy_label = f"policy {row.policy_id[:8]}..." if row.policy_id else "unknown policy"
        signals.append(_signal(
            signal_type="model_trigger_pattern",
            title=f"Model '{model_name}' repeatedly triggering '{policy_label}'",
            rationale=(
                f"The model '{model_name}' has triggered {policy_label} "
                f"{row.trigger_count} times (action: {row.decision}) in the past week. "
                f"This may indicate: (1) teams are unaware of the policy, "
                f"(2) the policy needs an exception for this model, or "
                f"(3) teams need guidance on model selection."
            ),
            severity="warning",
            evidence={
                "model": model_name,
                "policy_id": row.policy_id,
                "decision": row.decision,
                "trigger_count": row.trigger_count,
            },
            regulatory_tags=["eu_ai_act_article_14"],
        ))

    return signals


# ── Detector 3: Team Spend Anomaly ────────────────────────────────────────────

async def _detect_team_spend_anomaly(
    db: AsyncSession,
    team_id: str,
    since: datetime,
) -> list[dict]:
    """
    Detect sudden spend changes for a team.
    Signal: "This team's spend doubled overnight."
    """
    from orchestrator.db.models import UsageAggregate

    now = datetime.now(timezone.utc)
    recent_window = now - timedelta(days=1)
    baseline_start = now - timedelta(days=8)
    baseline_end = now - timedelta(days=1)

    # Recent 24h spend
    recent = await db.execute(
        select(func.coalesce(func.sum(UsageAggregate.total_cost), Decimal("0")))
        .where(
            UsageAggregate.team_id == team_id,
            UsageAggregate.period_start >= recent_window,
        )
    )
    recent_spend = float(recent.scalar_one() or 0)

    # Baseline daily average (previous 7 days)
    baseline = await db.execute(
        select(func.coalesce(func.sum(UsageAggregate.total_cost), Decimal("0")))
        .where(
            UsageAggregate.team_id == team_id,
            UsageAggregate.period_start >= baseline_start,
            UsageAggregate.period_start < baseline_end,
        )
    )
    baseline_total = float(baseline.scalar_one() or 0)
    baseline_daily_avg = baseline_total / 7.0 if baseline_total > 0 else 0

    if baseline_daily_avg <= 0 or recent_spend <= 0:
        return []

    ratio = recent_spend / baseline_daily_avg
    if ratio < 2.0:
        return []  # Less than 2x — not anomalous

    signals = [_signal(
        signal_type="team_spend_anomaly",
        title=f"Spend anomaly: {ratio:.1f}x baseline detected",
        rationale=(
            f"Today's spend (${recent_spend:.2f}) is {ratio:.1f}x the 7-day daily average "
            f"(${baseline_daily_avg:.2f}/day). This may indicate: unexpected model upgrades, "
            f"traffic spikes, prompt engineering changes, or a runaway process."
        ),
        severity="warning" if ratio < 3.0 else "critical",
        evidence={
            "recent_24h_spend": round(recent_spend, 4),
            "baseline_daily_avg": round(baseline_daily_avg, 4),
            "ratio": round(ratio, 2),
        },
        regulatory_tags=["nist_ai_rmf_measure_2"],
    )]

    return signals


# ── Detector 4: Enforcement Hotspots ──────────────────────────────────────────

async def _detect_enforcement_hotspots(
    db: AsyncSession,
    team_id: str,
    since: datetime,
) -> list[dict]:
    """
    Detect apps that are disproportionately hitting enforcement.
    Signal: "This app accounts for 80%+ of all policy violations."
    """
    from orchestrator.db.models import PolicyDecision, App

    # Count violations per app
    result = await db.execute(
        select(
            PolicyDecision.app_id,
            func.count(PolicyDecision.id).label("violation_count"),
        )
        .where(
            PolicyDecision.team_id == team_id,
            PolicyDecision.decided_at >= since,
            PolicyDecision.decision.in_(["deny", "throttle"]),
        )
        .group_by(PolicyDecision.app_id)
    )
    rows = result.all()

    if not rows:
        return []

    total_violations = sum(r.violation_count for r in rows)
    if total_violations < 20:
        return []  # Too few violations to detect patterns

    signals = []
    for row in rows:
        pct = (row.violation_count / total_violations) * 100
        if pct < 60:
            continue  # Not a hotspot

        # Look up app name
        app = await db.get(App, row.app_id) if row.app_id else None
        app_name = app.app_name if app else "Unknown app"

        signals.append(_signal(
            signal_type="enforcement_hotspot",
            title=f"Enforcement hotspot: {app_name} ({pct:.0f}% of violations)",
            rationale=(
                f"The app '{app_name}' accounts for {pct:.0f}% of all policy violations "
                f"({row.violation_count} of {total_violations} total). "
                f"This concentration suggests the app's AI usage patterns may need "
                f"review, or the app may need app-specific policy exceptions."
            ),
            severity="warning",
            evidence={
                "app_id": row.app_id,
                "app_name": app_name,
                "violation_count": row.violation_count,
                "total_violations": total_violations,
                "percentage": round(pct, 1),
            },
            regulatory_tags=["eu_ai_act_article_14", "nist_ai_rmf_measure_2"],
        ))

    return signals
