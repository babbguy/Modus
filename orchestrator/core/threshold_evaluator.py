"""
Modus — Threshold Evaluator
=====================================
Runs after each aggregation cycle to check configured thresholds against
current usage data. Fires Alert records and triggers integrations:

  - Notification channels (Slack, Teams, Email, PagerDuty, webhook)
  - Auto-pause callback (POSTs to app's registered pause endpoint)
  - Incident ticket creation (POSTs to Jira/ServiceNow/Linear webhook)

Design:
  - Dedup: won't re-fire the same threshold+period if an unacknowledged alert
    already exists for it.
  - Cooldown-free: new period = new evaluation window.
  - Non-blocking: notification dispatch is fire-and-forget via asyncio.create_task.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func as sa_func, select, update as sa_update

from orchestrator.core.config import settings
from orchestrator.db.models import (
    Alert, App, NotificationDelivery, Threshold, UsageAggregate,
)
from orchestrator.db.session import _session_factory

logger = logging.getLogger(__name__)


# ── Percentage-based alert tiers ──────────────────────────────────────────────
# Each threshold is evaluated against its critical_value at three percentage
# levels. An in-memory dict tracks which tiers have already fired for a given
# (threshold_id, period_start) to avoid duplicate alerts within the same window.

ALERT_TIERS = [
    (70, "caution", "Approaching budget threshold (70%)"),
    (90, "warning", "Nearing budget limit (90%)"),
    (100, "critical", "Budget threshold breached (100%)"),
]

# Key: (threshold_id, period_start_iso) → set of tier percentages already fired
_fired_tiers: dict[tuple[str, str], set[int]] = {}


def _fired_tier_key(threshold_id: str, period_start: datetime) -> tuple[str, str]:
    return (str(threshold_id), period_start.isoformat())


def _period_window(period: str, now: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) for the current evaluation window."""
    if period == "hourly":
        start = now.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    elif period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    elif period == "weekly":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start, start + timedelta(weeks=1)
    else:  # monthly
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            end = start.replace(year=now.year + 1, month=1)
        else:
            end = start.replace(month=now.month + 1)
        return start, end


_METRIC_COL_MAP = {
    "total_cost": "total_cost",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "call_count": "call_count",
}


async def evaluate_thresholds() -> int:
    """
    Check all active thresholds against current aggregated data.
    Returns the number of new alerts fired.
    """
    if _session_factory is None:
        return 0

    now = datetime.now(timezone.utc)

    # Purge stale _fired_tiers entries from previous periods to prevent
    # unbounded memory growth. An entry is stale if its period_start is
    # before the earliest possible current period (1 week ago covers all
    # period types: hourly, daily, weekly, monthly).
    cutoff = (now - timedelta(days=8)).isoformat()
    stale_keys = [k for k in _fired_tiers if k[1] < cutoff]
    for k in stale_keys:
        del _fired_tiers[k]

    fired = 0

    async with _session_factory() as db:
        thresholds = (await db.execute(
            select(Threshold).where(Threshold.is_active == True)
        )).scalars().all()

        for t in thresholds:
            try:
                fired += await _evaluate_one(db, t, now)
            except Exception:
                logger.error("Threshold eval failed", exc_info=True,
                             extra={"threshold_id": str(t.id)})

        if fired:
            await db.commit()

    if fired:
        logger.info("Threshold evaluation complete", extra={"alerts_fired": fired})
    return fired


async def _evaluate_one(db, t: Threshold, now: datetime) -> int:
    """
    Evaluate a single threshold using percentage-based alert tiers.

    Calculates spend as a percentage of critical_value and fires alerts at
    each crossed tier (70% caution, 90% warning, 100% critical) that hasn't
    already been alerted for this period.

    Returns the number of new alerts fired (0–3).
    """
    period_start, period_end = _period_window(t.period, now)

    # Map period to aggregation granularity
    if t.period == "hourly":
        granularity = "hourly"
    else:
        granularity = "daily"

    metric_col_name = _METRIC_COL_MAP.get(t.metric)
    if not metric_col_name:
        return 0

    metric_col = getattr(UsageAggregate, metric_col_name, None)
    if metric_col is None:
        return 0

    # Build query based on scope
    q = select(sa_func.coalesce(sa_func.sum(metric_col), 0))
    q = q.where(
        UsageAggregate.granularity == granularity,
        UsageAggregate.period_start >= period_start,
        UsageAggregate.period_start < period_end,
        UsageAggregate.team_id == str(t.team_id),
    )

    if t.scope == "app" and t.app_id:
        q = q.where(UsageAggregate.app_id == str(t.app_id))
    elif t.scope == "provider" and t.provider:
        q = q.where(UsageAggregate.provider == t.provider)
        if t.app_id:
            q = q.where(UsageAggregate.app_id == str(t.app_id))
    # scope == "team" → already filtered by team_id

    result = await db.execute(q)
    actual = Decimal(str(result.scalar() or 0))

    if not t.critical_value or t.critical_value == 0:
        return 0

    # Calculate percentage of critical_value reached
    pct = (actual / t.critical_value) * 100

    # Track which tiers we've already fired for this threshold+period
    tier_key = _fired_tier_key(t.id, period_start)
    already_fired = _fired_tiers.get(tier_key, set())

    fired = 0

    for tier_pct, tier_severity, tier_message in ALERT_TIERS:
        if pct < tier_pct:
            continue  # Haven't reached this tier yet

        if tier_pct in already_fired:
            continue  # Already fired for this tier in this period

        # For the 90% tier, use warning_value if explicitly set on the threshold
        if tier_pct == 90 and t.warning_value:
            threshold_value = t.warning_value
        elif tier_pct == 100:
            threshold_value = t.critical_value
        else:
            # 70% caution tier: compute the effective value
            threshold_value = (t.critical_value * tier_pct) / 100

        # Dedup: check for existing unacknowledged alert for same threshold+severity+period
        existing = (await db.execute(
            select(Alert.id).where(
                Alert.threshold_id == str(t.id),
                Alert.severity == tier_severity,
                Alert.period_start == period_start,
                Alert.acknowledged_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()

        if existing:
            # Mark as fired in memory too so we don't re-query next cycle
            already_fired.add(tier_pct)
            continue

        # Fire the alert
        alert = Alert(
            threshold_id=str(t.id),
            app_id=str(t.app_id) if t.app_id else None,
            team_id=str(t.team_id),
            severity=tier_severity,
            metric=t.metric,
            threshold_value=threshold_value,
            actual_value=actual,
            period_start=period_start,
            period_end=period_end,
        )
        db.add(alert)

        logger.warning(
            "Threshold tier breach: %s %s=%s (tier=%d%%, severity=%s, message=%s)",
            t.name, t.metric, actual, tier_pct, tier_severity, tier_message,
            extra={
                "threshold_id": str(t.id),
                "app_id": str(t.app_id) if t.app_id else None,
                "team_id": str(t.team_id),
                "severity": tier_severity,
                "tier_pct": tier_pct,
            },
        )

        # Fire-and-forget: dispatch notifications and integrations
        alert_data = {
            "alert_id": str(alert.id),
            "threshold_id": str(t.id),
            "threshold_name": t.name,
            "app_id": str(t.app_id) if t.app_id else None,
            "team_id": str(t.team_id),
            "severity": tier_severity,
            "metric": t.metric,
            "actual_value": str(actual),
            "threshold_value": str(threshold_value),
            "period": t.period,
            "tier_pct": tier_pct,
            "tier_message": tier_message,
        }

        asyncio.create_task(_dispatch_integrations(t, alert_data))

        already_fired.add(tier_pct)
        fired += 1

    # Persist fired tiers in memory
    if already_fired:
        _fired_tiers[tier_key] = already_fired

    return fired


async def _dispatch_integrations(threshold: Threshold, alert_data: dict) -> None:
    """
    Fire-and-forget: send notifications, auto-pause, and incident tickets.
    Runs as a background task — failures are logged but never block evaluation.
    """
    try:
        # 1. Send to configured notification channels
        await _send_notifications(alert_data)

        # 2. Auto-pause callback (critical only, opt-in)
        if (
            alert_data["severity"] == "critical"
            and settings.app_pause_endpoint_enabled
            and alert_data.get("app_id")
        ):
            await _trigger_auto_pause(alert_data)

        # 3. Incident ticket creation (critical only, if configured on threshold)
        notify_cfg = threshold.notify or {}
        incident_webhook = notify_cfg.get("incident_webhook")
        if alert_data["severity"] == "critical" and incident_webhook:
            await _trigger_incident_ticket(incident_webhook, alert_data)

    except Exception:
        logger.error("Integration dispatch failed", exc_info=True,
                     extra={"alert_id": alert_data.get("alert_id")})


async def _send_notifications(alert_data: dict) -> None:
    """Dispatch alert to all enabled notification channels."""
    from orchestrator.api import notifications as notif_mod
    from orchestrator.api.notifications import NotificationConfig, _load_config

    # The config cache is normally populated by the dashboard config endpoints;
    # after a process restart nothing else loads it, which previously meant
    # alerts were silently undelivered until an operator opened that page.
    # Fall back to a direct DB load so delivery never depends on the UI.
    if not notif_mod._cache_loaded:
        if _session_factory is None:
            return
        async with _session_factory() as db:
            await _load_config(db)

    if not notif_mod._config_cache:
        return

    config = NotificationConfig(**notif_mod._config_cache)
    severity = alert_data["severity"]

    # (channel_name, coroutine) so every outcome can be attributed to a channel
    # and durably recorded — a dropped alert is a compliance failure, not a log line.
    tasks: list[tuple[str, object]] = []

    if config.slack.enabled and config.slack.webhook_url:
        if _severity_meets_min(severity, config.slack.min_severity):
            tasks.append(("slack", _notify_slack(config.slack, alert_data)))

    if config.teams.enabled and config.teams.webhook_url:
        if _severity_meets_min(severity, config.teams.min_severity):
            tasks.append(("teams", _notify_teams(config.teams, alert_data)))

    if config.email.enabled and config.email.smtp_host and config.email.recipients:
        if _severity_meets_min(severity, config.email.min_severity):
            tasks.append(("email", _notify_email(config.email, alert_data)))

    if config.pagerduty.enabled and config.pagerduty.integration_key:
        if _severity_meets_min(severity, config.pagerduty.min_severity):
            tasks.append(("pagerduty", _notify_pagerduty(config.pagerduty, alert_data)))

    if config.webhook.enabled and config.webhook.url:
        if _severity_meets_min(severity, config.webhook.min_severity):
            tasks.append(("webhook", _notify_webhook(config.webhook, alert_data)))

    if not tasks:
        return

    results = await asyncio.gather(*[c for _, c in tasks], return_exceptions=True)

    deliveries: list[tuple[str, str, Optional[str]]] = []
    for (channel, _), r in zip(tasks, results):
        if isinstance(r, Exception):
            logger.warning(
                "Notification channel %s failed after retries — dead-lettered: %s",
                channel, r,
            )
            deliveries.append((channel, "dead_letter", str(r)[:1000]))
        else:
            deliveries.append((channel, "delivered", None))

    await _persist_deliveries(alert_data, severity, deliveries)


async def _persist_deliveries(
    alert_data: dict,
    severity: str,
    deliveries: "list[tuple[str, str, Optional[str]]]",
) -> None:
    """Durably record each channel's delivery outcome.

    Writes one NotificationDelivery row per channel. Dead-lettered notifications
    (bounded retries exhausted) are persisted WITH their payload so they can be
    audited and replayed — never silently dropped. The parent Alert's
    ``notification_sent`` / ``notification_result`` are updated so the dashboard
    reflects real delivery state instead of always showing "unsent".

    Best-effort: it must never break the fire-and-forget alert path, but a
    persistence failure is logged with context (zero silent failures).
    """
    if _session_factory is None:
        return
    alert_id = alert_data.get("alert_id")
    try:
        async with _session_factory() as db:
            any_delivered = False
            result_summary: dict[str, dict] = {}
            for channel, status, err in deliveries:
                if status == "delivered":
                    any_delivered = True
                db.add(NotificationDelivery(
                    alert_id=alert_id,
                    channel=channel,
                    severity=severity,
                    status=status,
                    last_error=err,
                    # Keep the payload only for dead-letters (enables replay);
                    # delivered rows don't need to duplicate the alert.
                    payload=alert_data if status == "dead_letter" else None,
                ))
                result_summary[channel] = {"status": status, "error": err}

            if alert_id:
                await db.execute(
                    sa_update(Alert)
                    .where(Alert.id == alert_id)
                    .values(
                        notification_sent=any_delivered,
                        notification_result=result_summary,
                    )
                )
            await db.commit()
    except Exception:
        logger.error(
            "Failed to persist notification deliveries",
            exc_info=True, extra={"alert_id": alert_id},
        )


def _severity_meets_min(actual: str, minimum: str) -> bool:
    order = {"caution": 0, "warning": 1, "critical": 2}
    return order.get(actual, 0) >= order.get(minimum, 0)


async def _notify_slack(config, alert_data: dict) -> None:
    severity_emoji = "\U0001f6a8" if alert_data["severity"] == "critical" else "\u26a0\ufe0f"
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{severity_emoji} Modus Alert: {alert_data['threshold_name']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*Severity:* {alert_data['severity'].upper()}\n"
            f"*Metric:* `{alert_data['metric']}` = `{alert_data['actual_value']}`\n"
            f"*Threshold:* `{alert_data['threshold_value']}`\n"
            f"*Period:* {alert_data['period']}"
        )}},
    ]
    await _http_post_retry(config.webhook_url, json={"blocks": blocks})


async def _notify_teams(config, alert_data: dict) -> None:
    card = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4", "body": [
                {"type": "TextBlock", "text": f"Modus Alert: {alert_data['threshold_name']}",
                    "weight": "Bolder", "size": "Medium"},
                {"type": "FactSet", "facts": [
                    {"title": "Severity", "value": alert_data["severity"].upper()},
                    {"title": "Metric", "value": f"{alert_data['metric']} = {alert_data['actual_value']}"},
                    {"title": "Threshold", "value": alert_data["threshold_value"]},
                ]},
            ]}}]}
    await _http_post_retry(config.webhook_url, json=card)


async def _notify_email(config, alert_data: dict) -> None:
    import smtplib
    from email.mime.text import MIMEText

    subject = f"[Modus] {alert_data['severity'].upper()}: {alert_data['threshold_name']}"
    body = (
        f"Threshold breach detected.\n\n"
        f"Threshold: {alert_data['threshold_name']}\n"
        f"Severity: {alert_data['severity'].upper()}\n"
        f"Metric: {alert_data['metric']} = {alert_data['actual_value']}\n"
        f"Limit: {alert_data['threshold_value']}\n"
        f"Period: {alert_data['period']}\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.from_address or "modus@localhost"
    msg["To"] = ", ".join(config.recipients)

    def _send():
        if config.smtp_use_tls:
            from orchestrator.core.tls import get_tls_context
            ctx = get_tls_context()
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as s:
                s.ehlo()
                s.starttls(context=ctx)
                if config.smtp_username:
                    s.login(config.smtp_username, config.smtp_password or "")
                s.sendmail(msg["From"], config.recipients, msg.as_string())
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as s:
                if config.smtp_username:
                    s.login(config.smtp_username, config.smtp_password or "")
                s.sendmail(msg["From"], config.recipients, msg.as_string())

    await asyncio.get_running_loop().run_in_executor(None, _send)


async def _http_post_retry(
    url: str, *, json: dict, headers: dict | None = None,
    retries: int = 3, base_backoff_s: float = 0.5,
) -> None:
    """POST with bounded exponential-backoff retry.

    A single dropped POST previously meant a silently lost alert. Retries
    transient network/5xx failures; a 4xx is a permanent client error and is
    not retried. Raises the last error if all attempts fail so the caller logs
    the channel failure.
    """
    import httpx
    from orchestrator.core.tls import get_httpx_client

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            async with get_httpx_client(timeout=10.0) as client:
                resp = await client.post(url, json=json, headers=headers)
            if resp.status_code < 400:
                return
            if resp.status_code < 500:
                # Permanent client error (bad webhook/key) — don't retry.
                raise httpx.HTTPStatusError(
                    f"{resp.status_code}", request=resp.request, response=resp
                )
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code}", request=resp.request, response=resp
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if getattr(getattr(exc, "response", None), "status_code", 500) < 500:
                raise  # 4xx — permanent
        if attempt < retries - 1:
            await asyncio.sleep(base_backoff_s * (2 ** attempt))
    if last_exc:
        raise last_exc


def _pagerduty_dedup_key(alert_data: dict) -> str:
    """Stable PagerDuty dedup key so repeated tier crossings for the same
    threshold correlate into ONE incident instead of opening a new one each
    time (and so it can be resolved on acknowledgement)."""
    import hashlib
    basis = f"{alert_data.get('threshold_id', '')}:{alert_data.get('metric', '')}"
    return "modus-" + hashlib.sha256(basis.encode()).hexdigest()[:32]


async def _notify_pagerduty(config, alert_data: dict) -> None:
    pd_payload = {
        "routing_key": config.integration_key,
        "event_action": "trigger",
        "dedup_key": _pagerduty_dedup_key(alert_data),
        "payload": {
            "summary": f"[Modus] {alert_data['severity'].upper()}: {alert_data['threshold_name']} — "
                       f"{alert_data['metric']}={alert_data['actual_value']}",
            "severity": alert_data["severity"],
            "source": config.service_name or "modus",
            "custom_details": alert_data,
        },
    }
    await _http_post_retry("https://events.pagerduty.com/v2/enqueue", json=pd_payload)


async def resolve_pagerduty(config, alert_data: dict) -> None:
    """Resolve the PagerDuty incident for a threshold once acknowledged/cleared.

    Uses the same dedup_key as the trigger so PagerDuty auto-resolves the
    matching incident."""
    pd_payload = {
        "routing_key": config.integration_key,
        "event_action": "resolve",
        "dedup_key": _pagerduty_dedup_key(alert_data),
    }
    await _http_post_retry("https://events.pagerduty.com/v2/enqueue", json=pd_payload)


async def _notify_webhook(config, alert_data: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if config.secret_header and config.secret_value:
        headers[config.secret_header] = config.secret_value
    await _http_post_retry(config.url, json=alert_data, headers=headers)


async def _trigger_auto_pause(alert_data: dict) -> None:
    """Look up the app's pause endpoint and POST to it."""
    if _session_factory is None:
        return
    app_id = alert_data.get("app_id")
    if not app_id:
        return

    async with _session_factory() as db:
        app = (await db.execute(
            select(App.app_name, App.pause_endpoint_url).where(App.id == app_id)
        )).one_or_none()

    if not app or not app.pause_endpoint_url:
        return

    from orchestrator.core.maintenance import notify_app_pause
    await notify_app_pause(
        app_id=app_id,
        app_name=app.app_name,
        pause_url=app.pause_endpoint_url,
        reason=f"Critical threshold breach: {alert_data['metric']}={alert_data['actual_value']}",
    )


async def _trigger_incident_ticket(webhook_url: str, alert_data: dict) -> None:
    """Create an incident ticket via webhook."""
    from orchestrator.core.maintenance import create_incident_ticket

    # Look up app name if we have an app_id
    app_name = None
    if alert_data.get("app_id") and _session_factory:
        async with _session_factory() as db:
            row = (await db.execute(
                select(App.app_name).where(App.id == alert_data["app_id"])
            )).scalar_one_or_none()
            app_name = row

    alert_data_enriched = {**alert_data, "app_name": app_name or alert_data.get("app_id", "unknown")}
    await create_incident_ticket(webhook_url, alert_data_enriched)
