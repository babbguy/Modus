"""
Modus — Notifications Router
=====================================
Manages notification channel configuration and provides a test-fire endpoint.

GET    /api/v1/notifications/config          — get current notification config
PUT    /api/v1/notifications/config          — save notification config
POST   /api/v1/notifications/test            — fire a test alert to configured channels
POST   /api/v1/notifications/test/{channel}  — fire a test alert to one channel only
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.models import AuditLog, NotificationChannelConfig
from orchestrator.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])

# ── Config store (DB-backed with in-memory cache) ─────────────────────────────
_config_cache: dict[str, Any] = {}
_cache_loaded: bool = False


async def _load_config(db: AsyncSession) -> dict[str, Any]:
    """Load notification config from DB, caching in memory."""
    global _config_cache, _cache_loaded
    if _cache_loaded:
        return _config_cache

    result = await db.execute(
        select(NotificationChannelConfig).where(
            NotificationChannelConfig.scope == "global"
        )
    )
    row = result.scalar_one_or_none()
    if row and row.config_json:
        import json
        _config_cache = json.loads(row.config_json)
    else:
        _config_cache = {}
    _cache_loaded = True
    return _config_cache


async def _save_config(db: AsyncSession, config: dict, actor_id: str | None = None) -> None:
    """Persist notification config to DB and update cache."""
    global _config_cache, _cache_loaded
    import json

    result = await db.execute(
        select(NotificationChannelConfig).where(
            NotificationChannelConfig.scope == "global"
        )
    )
    row = result.scalar_one_or_none()

    config_json = json.dumps(config)
    if row:
        row.config_json = config_json
        row.updated_by = actor_id
    else:
        db.add(NotificationChannelConfig(
            scope="global",
            config_json=config_json,
            updated_by=actor_id,
        ))

    _config_cache = config
    _cache_loaded = True


# ── Schemas ────────────────────────────────────────────────────────────────────

class SlackConfig(BaseModel):
    enabled: bool = False
    webhook_url: Optional[str] = Field(None, description="Slack incoming webhook URL")
    channel: Optional[str] = Field(None, description="Override channel (e.g. #alerts)")
    min_severity: Literal["warning", "critical"] = "warning"


class TeamsConfig(BaseModel):
    enabled: bool = False
    webhook_url: Optional[str] = Field(None, description="Teams Power Automate webhook URL")
    min_severity: Literal["warning", "critical"] = "warning"


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    from_address: Optional[str] = None
    recipients: list[str] = Field(default_factory=list)
    min_severity: Literal["warning", "critical"] = "warning"


class PagerDutyConfig(BaseModel):
    enabled: bool = False
    integration_key: Optional[str] = None
    service_name: Optional[str] = None
    min_severity: Literal["warning", "critical"] = "critical"


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: Optional[str] = None
    secret_header: Optional[str] = None
    secret_value: Optional[str] = None
    min_severity: Literal["warning", "critical"] = "warning"


class NotificationConfig(BaseModel):
    slack: SlackConfig = Field(default_factory=SlackConfig)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    pagerduty: PagerDutyConfig = Field(default_factory=PagerDutyConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)


# Secret fields are never returned to clients. GET/PUT responses carry this
# sentinel instead; a PUT that sends the sentinel back means "keep the stored
# value", so the dashboard can save the form without re-typing credentials.
_SECRET_SENTINEL = "********"
_SECRET_FIELDS = (
    ("email", "smtp_password"),
    ("pagerduty", "integration_key"),
    ("webhook", "secret_value"),
)


def _mask_secrets(config_data: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy config with every non-empty secret replaced by the sentinel."""
    import copy

    masked = copy.deepcopy(config_data)
    for channel, field in _SECRET_FIELDS:
        if masked.get(channel, {}).get(field):
            masked[channel][field] = _SECRET_SENTINEL
    return masked


def _restore_secrets(new_data: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Replace sentinel-valued secrets in an incoming config with stored values."""
    for channel, field in _SECRET_FIELDS:
        if new_data.get(channel, {}).get(field) == _SECRET_SENTINEL:
            new_data[channel][field] = stored.get(channel, {}).get(field)
    return new_data


class TestResult(BaseModel):
    channel: str
    success: bool
    message: str
    latency_ms: Optional[float] = None


class TestResponse(BaseModel):
    results: list[TestResult]
    tested_at: datetime


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/config", response_model=NotificationConfig, summary="Get notification config")
async def get_config(
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> NotificationConfig:
    """Return current notification channel configuration (DB-persisted).

    Secret fields (SMTP password, PagerDuty key, webhook secret) are masked.
    """
    stored = await _load_config(db)
    return NotificationConfig(**_mask_secrets(stored)) if stored else NotificationConfig()


@router.put(
    "/config",
    response_model=NotificationConfig,
    summary="Save notification config",
)
async def save_config(
    body: NotificationConfig,
    request: Request,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_session, scope="function"),
) -> NotificationConfig:
    """
    Persist notification channel configuration.
    Sensitive fields (passwords, keys) are accepted but never returned in GET responses.
    """
    identity.assert_permission("notifications:write")

    config_data = body.model_dump()

    # A sentinel-valued secret means "keep what's stored" — the dashboard
    # round-trips masked GET responses back through PUT.
    stored = await _load_config(db)
    config_data = _restore_secrets(config_data, stored)

    # Persist to DB
    await _save_config(db, config_data, actor_id=identity.actor_id)

    # Audit log — deep-copy masking; a shallow copy here would mutate the
    # nested dicts shared with the live cache and corrupt stored secrets.
    safe = _mask_secrets(config_data)

    db.add(AuditLog(
        actor_id=identity.actor_id,
        actor_ip=request.client.host if request.client else None,
        team_id=None,
        resource_type="notification_config",
        resource_id="global",
        action="updated",
        after=safe,
    ))

    logger.info("Notification config saved to DB", extra={"actor": identity.actor_id})
    return NotificationConfig(**_mask_secrets(config_data))


@router.post(
    "/test",
    response_model=TestResponse,
    summary="Send test notification to all enabled channels",
)
async def test_all_channels(
    request: Request,
    identity: Identity = Depends(get_identity),
) -> TestResponse:
    """
    Fire a test alert payload to every enabled channel.
    Returns per-channel results so the user can see exactly what succeeded or failed.
    """
    # Re-load config from cache (already loaded by prior GET/PUT call or startup)
    config = NotificationConfig(**_config_cache) if _config_cache else NotificationConfig()

    test_payload = _build_test_payload()
    tasks = []

    if config.slack.enabled and config.slack.webhook_url:
        tasks.append(("slack", _test_slack(config.slack, test_payload)))
    if config.teams.enabled and config.teams.webhook_url:
        tasks.append(("teams", _test_teams(config.teams, test_payload)))
    if config.email.enabled and config.email.smtp_host and config.email.recipients:
        tasks.append(("email", _test_email(config.email, test_payload)))
    if config.pagerduty.enabled and config.pagerduty.integration_key:
        tasks.append(("pagerduty", _test_pagerduty(config.pagerduty, test_payload)))
    if config.webhook.enabled and config.webhook.url:
        tasks.append(("webhook", _test_generic_webhook(config.webhook, test_payload)))

    if not tasks:
        return TestResponse(
            results=[TestResult(
                channel="none",
                success=False,
                message="No channels are enabled or configured. Save your notification config first.",
            )],
            tested_at=datetime.now(timezone.utc),
        )

    results = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)

    test_results = []
    for (channel, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            test_results.append(TestResult(channel=channel, success=False, message=str(result)))
        else:
            test_results.append(result)

    return TestResponse(results=test_results, tested_at=datetime.now(timezone.utc))


@router.post(
    "/test/{channel_name}",
    response_model=TestResponse,
    summary="Send test notification to a single channel",
)
async def test_one_channel(
    channel_name: str,
    identity: Identity = Depends(get_identity),
) -> TestResponse:
    """Fire a test alert to one specific channel by name."""
    valid_channels = {"slack", "teams", "email", "pagerduty", "webhook"}
    if channel_name not in valid_channels:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown channel '{channel_name}'. Valid: {sorted(valid_channels)}",
        )

    config = NotificationConfig(**_config_cache) if _config_cache else NotificationConfig()
    test_payload = _build_test_payload()
    result: Optional[TestResult] = None

    try:
        if channel_name == "slack":
            if not config.slack.webhook_url:
                raise ValueError("Slack webhook URL not configured.")
            result = await _test_slack(config.slack, test_payload)
        elif channel_name == "teams":
            if not config.teams.webhook_url:
                raise ValueError("Teams webhook URL not configured.")
            result = await _test_teams(config.teams, test_payload)
        elif channel_name == "email":
            if not config.email.smtp_host or not config.email.recipients:
                raise ValueError("Email SMTP host and recipients must be configured.")
            result = await _test_email(config.email, test_payload)
        elif channel_name == "pagerduty":
            if not config.pagerduty.integration_key:
                raise ValueError("PagerDuty integration key not configured.")
            result = await _test_pagerduty(config.pagerduty, test_payload)
        elif channel_name == "webhook":
            if not config.webhook.url:
                raise ValueError("Webhook URL not configured.")
            result = await _test_generic_webhook(config.webhook, test_payload)
    except Exception as exc:
        result = TestResult(channel=channel_name, success=False, message=str(exc))

    return TestResponse(
        results=[result] if result else [],
        tested_at=datetime.now(timezone.utc),
    )


# ── Channel dispatch helpers ───────────────────────────────────────────────────

def _build_test_payload() -> dict:
    return {
        "type": "test",
        "severity": "warning",
        "metric": "total_cost",
        "threshold_value": "50.00",
        "actual_value": "67.43",
        "app_id": "example-app",
        "team": "platform",
        "message": "Modus test notification — your alerts are working correctly.",
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "dashboard_url": "http://localhost:3001",
    }


async def _test_slack(config: SlackConfig, payload: dict) -> TestResult:
    import time
    start = time.monotonic()
    try:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🧪 Modus Test Alert"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{payload['message']}*\n\n"
                        f"This confirms Slack alerting is correctly configured.\n"
                        f"Threshold: `${payload['threshold_value']}` | "
                        f"Actual: `${payload['actual_value']}`"
                    ),
                },
            },
        ]
        body: dict = {"blocks": blocks}
        if config.channel:
            body["channel"] = config.channel

        from orchestrator.core.tls import get_httpx_client

        async with get_httpx_client(timeout=10.0) as client:
            resp = await client.post(config.webhook_url, json=body)

        latency = (time.monotonic() - start) * 1000
        if resp.status_code == 200 and resp.text == "ok":
            return TestResult(channel="slack", success=True, message="Delivered ✓", latency_ms=latency)
        return TestResult(
            channel="slack", success=False,
            message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            latency_ms=latency,
        )
    except Exception as exc:
        return TestResult(channel="slack", success=False, message=str(exc))


async def _test_teams(config: TeamsConfig, payload: dict) -> TestResult:
    import time
    start = time.monotonic()
    try:
        # Teams Adaptive Card via Power Automate webhook
        card = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "🧪 Modus Test Alert",
                            "weight": "Bolder",
                            "size": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": payload["message"],
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Threshold", "value": f"${payload['threshold_value']}"},
                                {"title": "Actual", "value": f"${payload['actual_value']}"},
                                {"title": "App", "value": payload["app_id"]},
                            ],
                        },
                    ],
                },
            }],
        }
        from orchestrator.core.tls import get_httpx_client

        async with get_httpx_client(timeout=10.0) as client:
            resp = await client.post(config.webhook_url, json=card)
        latency = (time.monotonic() - start) * 1000
        if resp.status_code in (200, 202):
            return TestResult(channel="teams", success=True, message="Delivered ✓", latency_ms=latency)
        return TestResult(
            channel="teams", success=False,
            message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            latency_ms=latency,
        )
    except Exception as exc:
        return TestResult(channel="teams", success=False, message=str(exc))


async def _test_email(config: EmailConfig, payload: dict) -> TestResult:
    import time
    start = time.monotonic()
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🧪 Modus Test Alert"
        msg["From"] = config.from_address or "modus@localhost"
        msg["To"] = ", ".join(config.recipients)

        text_body = (
            f"Modus Test Notification\n\n"
            f"{payload['message']}\n\n"
            f"Threshold: ${payload['threshold_value']}\n"
            f"Actual: ${payload['actual_value']}\n"
            f"App: {payload['app_id']}\n"
            f"Fired: {payload['fired_at']}\n"
        )
        html_body = f"""
        <html><body style="font-family: sans-serif; color: #1a1a2e;">
          <h2>🧪 Modus Test Alert</h2>
          <p>{payload['message']}</p>
          <table>
            <tr><td><b>Threshold:</b></td><td>${payload['threshold_value']}</td></tr>
            <tr><td><b>Actual:</b></td><td>${payload['actual_value']}</td></tr>
            <tr><td><b>App:</b></td><td>{payload['app_id']}</td></tr>
            <tr><td><b>Fired:</b></td><td>{payload['fired_at']}</td></tr>
          </table>
        </body></html>
        """
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        # SMTP send in a thread to avoid blocking the event loop
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

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send)
        latency = (time.monotonic() - start) * 1000
        return TestResult(
            channel="email", success=True,
            message=f"Sent to {len(config.recipients)} recipient(s) ✓",
            latency_ms=latency,
        )
    except Exception as exc:
        return TestResult(channel="email", success=False, message=str(exc))


async def _test_pagerduty(config: PagerDutyConfig, payload: dict) -> TestResult:
    import time
    start = time.monotonic()
    try:
        pd_payload = {
            "routing_key": config.integration_key,
            "event_action": "trigger",
            "payload": {
                "summary": f"[TEST] Modus: {payload['message']}",
                "severity": "warning",
                "source": config.service_name or "modus",
                "custom_details": {
                    "threshold": payload["threshold_value"],
                    "actual": payload["actual_value"],
                    "app_id": payload["app_id"],
                    "note": "This is a test event from Modus.",
                },
            },
        }
        from orchestrator.core.tls import get_httpx_client

        async with get_httpx_client(timeout=10.0) as client:
            resp = await client.post(
                "https://events.pagerduty.com/v2/enqueue",
                json=pd_payload,
            )
        latency = (time.monotonic() - start) * 1000
        if resp.status_code in (200, 202):
            return TestResult(channel="pagerduty", success=True, message="Delivered ✓", latency_ms=latency)
        return TestResult(
            channel="pagerduty", success=False,
            message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            latency_ms=latency,
        )
    except Exception as exc:
        return TestResult(channel="pagerduty", success=False, message=str(exc))


async def _test_generic_webhook(config: WebhookConfig, payload: dict) -> TestResult:
    import time
    start = time.monotonic()
    try:
        headers = {"Content-Type": "application/json"}
        if config.secret_header and config.secret_value:
            headers[config.secret_header] = config.secret_value

        from orchestrator.core.tls import get_httpx_client

        async with get_httpx_client(timeout=10.0) as client:
            resp = await client.post(config.url, json=payload, headers=headers)

        latency = (time.monotonic() - start) * 1000
        if resp.status_code < 400:
            return TestResult(
                channel="webhook", success=True,
                message=f"HTTP {resp.status_code} ✓",
                latency_ms=latency,
            )
        return TestResult(
            channel="webhook", success=False,
            message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            latency_ms=latency,
        )
    except Exception as exc:
        return TestResult(channel="webhook", success=False, message=str(exc))
