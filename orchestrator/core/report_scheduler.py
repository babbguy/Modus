"""
Modus — Finance Report Scheduler
======================================
Background task that generates and delivers scheduled finance reports.

Reports are delivered via webhook (customer's own endpoint — Slack, Teams,
PagerDuty, ServiceNow, custom). No SMTP dependency, no external email service.

Delivery channels:
    - webhook: POST JSON or CSV payload to any URL
    - slack: POST formatted message to Slack incoming webhook

Schedule evaluation:
    - daily: last_run_at is None or last_run_at < today
    - weekly: last_run_at is None or last_run_at < this Monday
    - monthly: last_run_at is None or last_run_at < 1st of this month
    - quarterly: last_run_at is None or last_run_at < 1st of this quarter

All data stays on customer infrastructure (Law 4). Webhook targets are
customer-configured URLs on their own network.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib import request as urllib_request

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    FinanceReport, Team, AuditLog,
)
from orchestrator.db.session import _session_factory, sqlite_dt

logger = logging.getLogger(__name__)


# ── Schedule Evaluation ──────────────────────────────────────────────────────


def _is_due(report: FinanceReport, now: datetime) -> bool:
    """Check if a report is due to run based on its schedule."""
    if not report.is_active:
        return False
    last = report.last_run_at
    if last is None:
        return True

    if report.schedule == "daily":
        return last.date() < now.date()
    elif report.schedule == "weekly":
        # Due if last run was before this Monday
        this_monday = now - timedelta(days=now.weekday())
        this_monday = this_monday.replace(hour=0, minute=0, second=0, microsecond=0)
        return last < this_monday
    elif report.schedule == "monthly":
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return last < month_start
    elif report.schedule == "quarterly":
        q_start_month = ((now.month - 1) // 3) * 3 + 1
        q_start = now.replace(month=q_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        return last < q_start

    return False


# ── Report Data Generators ───────────────────────────────────────────────────


async def _generate_chargeback(
    db: AsyncSession, cost_center_id: Optional[str], now: datetime
) -> list[dict[str, Any]]:
    """Generate chargeback data for the previous period."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    q = text("""
        SELECT
            t.slug AS team_slug, t.name AS team_name,
            COALESCE(cc.code, 'UNASSIGNED') AS cost_center_code,
            COALESCE(cc.department, 'Unassigned') AS department,
            a.app_name, ua.provider, ua.model,
            SUM(ua.call_count) AS calls,
            SUM(ua.total_tokens) AS tokens,
            SUM(ua.total_cost) AS cost
        FROM usage_aggregates ua
        JOIN apps a ON a.id = ua.app_id
        JOIN teams t ON t.id = ua.team_id
        LEFT JOIN team_cost_centers tcc ON tcc.team_id = t.id
        LEFT JOIN cost_centers cc ON cc.id = tcc.cost_center_id
        WHERE ua.granularity = 'daily'
          AND ua.period_start >= :start AND ua.period_start < :end
          AND (CAST(:cc_id AS TEXT) IS NULL OR CAST(tcc.cost_center_id AS TEXT) = CAST(:cc_id AS TEXT))
        GROUP BY t.slug, t.name, cc.code, cc.department, a.app_name, ua.provider, ua.model
        ORDER BY SUM(ua.total_cost) DESC
    """)
    result = await db.execute(q, {
        "start": sqlite_dt(month_start), "end": sqlite_dt(now), "cc_id": cost_center_id,
    })
    return [
        {
            "team_slug": r.team_slug,
            "team_name": r.team_name,
            "cost_center": r.cost_center_code,
            "department": r.department,
            "app": r.app_name,
            "provider": r.provider,
            "model": r.model,
            "calls": int(r.calls or 0),
            "tokens": int(r.tokens or 0),
            "cost_usd": round(float(r.cost or 0), 2),
        }
        for r in result.all()
    ]


async def _generate_burn_rate(
    db: AsyncSession, cost_center_id: Optional[str], now: datetime
) -> list[dict[str, Any]]:
    """Generate burn rate data with EOM projections."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_elapsed = max((now - month_start).days, 1)

    q = text("""
        SELECT t.slug, t.name, t.budget_monthly_usd,
               COALESCE(SUM(ua.total_cost), 0) AS spend
        FROM teams t
        LEFT JOIN usage_aggregates ua ON ua.team_id = t.id
          AND ua.period_start >= :start AND ua.period_start < :end
        WHERE t.deleted_at IS NULL
          AND t.budget_monthly_usd IS NOT NULL
          AND t.budget_monthly_usd > 0
        GROUP BY t.id, t.slug, t.name, t.budget_monthly_usd
        ORDER BY COALESCE(SUM(ua.total_cost), 0) DESC
    """)
    result = await db.execute(q, {"start": sqlite_dt(month_start), "end": sqlite_dt(now)})

    entries = []
    for r in result.all():
        spend = float(r.spend or 0)
        budget = float(r.budget_monthly_usd or 0)
        daily_rate = spend / days_elapsed
        projected_eom = daily_rate * 30
        burn_pct = (spend / budget * 100) if budget > 0 else 0

        entries.append({
            "team": r.slug,
            "team_name": r.name,
            "budget_usd": round(budget, 2),
            "spend_usd": round(spend, 2),
            "projected_eom_usd": round(projected_eom, 2),
            "burn_pct": round(burn_pct, 1),
            "risk": "over-budget" if burn_pct >= 100 else "at-risk" if burn_pct >= 80 else "on-track",
            "days_remaining": 30 - days_elapsed,
        })
    return entries


async def _generate_variance(
    db: AsyncSession, cost_center_id: Optional[str], now: datetime
) -> list[dict[str, Any]]:
    """Month-over-month spend variance by team."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month > 1:
        prev_start = month_start.replace(month=now.month - 1)
    else:
        prev_start = month_start.replace(year=now.year - 1, month=12)
    prev_end = month_start

    # Current month spend per team
    cur_q = text("""
        SELECT team_id, SUM(total_cost) AS cost FROM usage_aggregates
        WHERE granularity = 'daily' AND period_start >= :start AND period_start < :end
        GROUP BY team_id
    """)
    cur_result = await db.execute(cur_q, {"start": sqlite_dt(month_start), "end": sqlite_dt(now)})
    current = {r.team_id: float(r.cost or 0) for r in cur_result.all()}

    # Previous month spend per team
    prev_result = await db.execute(cur_q, {"start": sqlite_dt(prev_start), "end": sqlite_dt(prev_end)})
    previous = {r.team_id: float(r.cost or 0) for r in prev_result.all()}

    # Team names
    team_result = await db.execute(
        select(Team).where(Team.deleted_at.is_(None))
    )
    teams = {str(t.id): t for t in team_result.scalars().all()}

    all_ids = set(current.keys()) | set(previous.keys())
    entries = []
    for tid in all_ids:
        team = teams.get(tid)
        cur_val = current.get(tid, 0)
        prev_val = previous.get(tid, 0)
        delta = cur_val - prev_val
        pct = ((delta / prev_val) * 100) if prev_val > 0 else 0

        entries.append({
            "team_id": tid,
            "team_name": team.name if team else tid,
            "current_month_usd": round(cur_val, 2),
            "previous_month_usd": round(prev_val, 2),
            "delta_usd": round(delta, 2),
            "delta_pct": round(pct, 1),
        })

    entries.sort(key=lambda x: abs(x["delta_usd"]), reverse=True)
    return entries


async def _generate_audit_trail(
    db: AsyncSession, cost_center_id: Optional[str], now: datetime
) -> list[dict[str, Any]]:
    """Export recent audit trail entries."""
    since = now - timedelta(days=30)
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.occurred_at >= since)
        .order_by(AuditLog.occurred_at.desc())
        .limit(1000)
    )
    return [
        {
            "timestamp": log.occurred_at.isoformat(),
            "actor": log.actor_id,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "team_id": log.team_id,
        }
        for log in result.scalars().all()
    ]


async def _generate_forecast_report(
    db: AsyncSession, cost_center_id: Optional[str], now: datetime
) -> list[dict[str, Any]]:
    """Generate forecast summaries for all teams with budgets."""
    from orchestrator.core.forecast_engine import builtin_forecast, predict_budget_breach

    window_start = now - timedelta(days=28)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if now.month < 12:
        month_end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    else:
        month_end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    days_remaining = max(0, (month_end - now).days)

    teams_result = await db.execute(
        select(Team).where(
            Team.deleted_at.is_(None),
            Team.budget_monthly_usd.isnot(None),
        )
    )
    entries = []

    for team in teams_result.scalars().all():
        tid = str(team.id)

        # Daily costs
        daily_result = await db.execute(text("""
            SELECT DATE(period_start) AS day, SUM(total_cost) AS cost
            FROM usage_aggregates
            WHERE granularity = 'daily' AND team_id = :tid
              AND period_start >= :start AND period_start < :end
            GROUP BY DATE(period_start) ORDER BY DATE(period_start)
        """), {"tid": tid, "start": sqlite_dt(window_start), "end": sqlite_dt(now)})
        daily_costs = [float(r.cost or 0) for r in daily_result.all()]

        # MTD
        mtd_result = await db.execute(text("""
            SELECT COALESCE(SUM(total_cost), 0) FROM usage_aggregates
            WHERE granularity = 'daily' AND team_id = :tid
              AND period_start >= :start AND period_start < :end
        """), {"tid": tid, "start": sqlite_dt(month_start), "end": sqlite_dt(now)})
        mtd = float(mtd_result.scalar() or 0)

        budget = float(team.budget_monthly_usd or 0)

        if len(daily_costs) >= 3:
            forecast = builtin_forecast(
                daily_costs=daily_costs,
                days_remaining_month=days_remaining,
                days_remaining_quarter=days_remaining,  # simplified for reports
                days_remaining_year=days_remaining,
                mtd_actual=mtd,
            )
            breach = predict_budget_breach(daily_costs, budget, mtd, now.day)
        else:
            forecast = {"forecast_eom": mtd, "method": "insufficient_data"}
            breach = None

        entries.append({
            "team": team.slug,
            "team_name": team.name,
            "budget_usd": round(budget, 2),
            "mtd_actual_usd": round(mtd, 2),
            "forecast_eom_usd": forecast.get("forecast_eom", mtd),
            "method": forecast.get("method", "builtin"),
            "breach_predicted": breach is not None,
            "breach_date": breach.get("breach_date") if breach else None,
            "breach_confidence": breach.get("confidence") if breach else None,
        })

    entries.sort(key=lambda x: x.get("forecast_eom_usd", 0), reverse=True)
    return entries


_GENERATORS = {
    "chargeback": _generate_chargeback,
    "burn_rate": _generate_burn_rate,
    "variance": _generate_variance,
    "audit_trail": _generate_audit_trail,
    "forecast": _generate_forecast_report,
}


# ── Delivery ─────────────────────────────────────────────────────────────────


def _to_csv(data: list[dict]) -> str:
    """Convert list of dicts to CSV string."""
    if not data:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys())
    writer.writeheader()
    writer.writerows(data)
    return output.getvalue()


def _format_slack_message(report: FinanceReport, data: list[dict], now: datetime) -> dict:
    """Format report data as a Slack incoming webhook payload."""
    summary_lines = []
    total = sum(row.get("cost_usd", row.get("spend_usd", 0)) for row in data[:10])

    if report.report_type == "burn_rate":
        at_risk = sum(1 for r in data if r.get("risk") in ("at-risk", "over-budget"))
        summary_lines.append(f"*{len(data)} teams tracked* | *{at_risk} at risk*")
        for row in data[:5]:
            emoji = ":red_circle:" if row.get("risk") == "over-budget" else ":large_orange_circle:" if row.get("risk") == "at-risk" else ":large_green_circle:"
            summary_lines.append(
                f"{emoji} {row['team_name']}: ${row['spend_usd']:,.2f} / ${row['budget_usd']:,.2f} ({row['burn_pct']}%)"
            )
    elif report.report_type == "forecast":
        breaches = sum(1 for r in data if r.get("breach_predicted"))
        summary_lines.append(f"*{len(data)} teams forecasted* | *{breaches} breach warnings*")
        for row in data[:5]:
            emoji = ":warning:" if row.get("breach_predicted") else ":chart_with_upwards_trend:"
            breach_note = f" — breach by {row['breach_date']}" if row.get("breach_date") else ""
            summary_lines.append(
                f"{emoji} {row['team_name']}: ${row.get('forecast_eom_usd', 0):,.2f} EOM{breach_note}"
            )
    elif report.report_type == "variance":
        for row in data[:5]:
            emoji = ":arrow_up:" if row.get("delta_usd", 0) > 0 else ":arrow_down:"
            summary_lines.append(
                f"{emoji} {row['team_name']}: {row['delta_pct']:+.1f}% (${row['delta_usd']:+,.2f})"
            )
    else:
        summary_lines.append(f"*{len(data)} records* | Total: ${total:,.2f}")

    return {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Modus: {report.name}"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(summary_lines),
                }
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')} | {report.schedule} {report.report_type} report"}
                ]
            }
        ]
    }


async def _deliver_webhook(
    url: str, payload: bytes, content_type: str = "application/json"
) -> bool:
    """POST payload to a webhook URL. Returns True on success."""
    try:
        req = urllib_request.Request(
            url,
            data=payload,
            headers={"Content-Type": content_type},
            method="POST",
        )

        def _call():
            with urllib_request.urlopen(req, timeout=30) as resp:
                return 200 <= resp.status < 300

        return await asyncio.to_thread(_call)
    except Exception as exc:
        logger.warning("Webhook delivery failed to %s: %s", url, exc)
        return False


# ── Generate + Deliver ───────────────────────────────────────────────────────


async def generate_and_deliver_report(report: FinanceReport) -> bool:
    """
    Generate report data and deliver it. Returns True on success.
    Called by the background scheduler and by the manual /run endpoint.
    """
    if _session_factory is None:
        return False

    now = datetime.now(timezone.utc)
    generator = _GENERATORS.get(report.report_type)
    if not generator:
        logger.error("Unknown report type: %s", report.report_type)
        return False

    try:
        async with _session_factory() as db:
            data = await generator(db, report.cost_center_id, now)

            # Format payload
            if report.delivery_channel == "slack":
                payload_dict = _format_slack_message(report, data, now)
                payload = json.dumps(payload_dict).encode()
                content_type = "application/json"
            elif report.format == "csv":
                csv_str = _to_csv(data)
                payload = json.dumps({
                    "report_name": report.name,
                    "report_type": report.report_type,
                    "generated_at": now.isoformat(),
                    "record_count": len(data),
                    "format": "csv",
                    "data_csv": csv_str,
                }).encode()
                content_type = "application/json"
            else:
                payload = json.dumps({
                    "report_name": report.name,
                    "report_type": report.report_type,
                    "generated_at": now.isoformat(),
                    "record_count": len(data),
                    "data": data,
                }).encode()
                content_type = "application/json"

            # Deliver
            success = await _deliver_webhook(
                report.delivery_target, payload, content_type
            )

            if success:
                report.last_run_at = now
                await db.commit()
                logger.info(
                    "Report delivered: %s (%s → %s)",
                    report.name, report.report_type, report.delivery_channel,
                )
            else:
                logger.warning(
                    "Report delivery failed: %s → %s",
                    report.name, report.delivery_target,
                )

            return success

    except Exception as exc:
        logger.error("Report generation failed: %s — %s", report.name, exc)
        return False


# ── Background Task Loop ────────────────────────────────────────────────────


async def run_report_scheduler() -> None:
    """
    Check all active finance reports and run any that are due.
    Called periodically by the main task loop.
    """
    if _session_factory is None:
        return

    now = datetime.now(timezone.utc)
    start = time.perf_counter()

    try:
        async with _session_factory() as db:
            result = await db.execute(
                select(FinanceReport).where(FinanceReport.is_active == True)
            )
            reports = result.scalars().all()

            delivered = 0
            for report in reports:
                if _is_due(report, now):
                    success = await generate_and_deliver_report(report)
                    if success:
                        delivered += 1

        elapsed = time.perf_counter() - start
        if delivered > 0:
            logger.info(
                "Report scheduler: %d reports delivered in %.0fms",
                delivered, elapsed * 1000,
            )

    except Exception as exc:
        logger.error("Report scheduler failed: %s", exc)
