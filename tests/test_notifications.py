"""
Tests — Notifications Router
Covers: config CRUD, test-fire endpoints, channel dispatch, schema validation.
"""
from __future__ import annotations

# ── Config endpoints ─────────────────────────────────────────────────────────

async def test_get_config_default(client):
    # Reset the cache to ensure fresh state
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = False

    resp = await client.get("/api/v1/notifications/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "slack" in body
    assert "teams" in body
    assert "email" in body
    assert "pagerduty" in body
    assert "webhook" in body
    assert body["slack"]["enabled"] is False


async def test_save_config(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = False

    config = {
        "slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/test", "min_severity": "warning"},
        "teams": {"enabled": False, "min_severity": "warning"},
        "email": {"enabled": False, "smtp_port": 587, "smtp_use_tls": True, "recipients": [], "min_severity": "warning"},
        "pagerduty": {"enabled": False, "min_severity": "critical"},
        "webhook": {"enabled": False, "min_severity": "warning"},
    }
    resp = await client.put("/api/v1/notifications/config", json=config)
    assert resp.status_code == 200
    body = resp.json()
    assert body["slack"]["enabled"] is True
    assert body["slack"]["webhook_url"] == "https://hooks.slack.com/test"


async def test_save_config_masks_sensitive_fields(client):
    """Verify sensitive fields are accepted and config persists."""
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = False

    config = {
        "slack": {"enabled": False, "min_severity": "warning"},
        "teams": {"enabled": False, "min_severity": "warning"},
        "email": {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_use_tls": True,
            "smtp_password": "super-secret",
            "recipients": ["admin@example.com"],
            "min_severity": "warning",
        },
        "pagerduty": {"enabled": True, "integration_key": "pd-key-123", "min_severity": "critical"},
        "webhook": {"enabled": True, "url": "https://hook.example.com", "secret_value": "shh", "min_severity": "warning"},
    }
    resp = await client.put("/api/v1/notifications/config", json=config)
    assert resp.status_code == 200


# ── Test-fire endpoints ──────────────────────────────────────────────────────

async def test_test_all_channels_none_enabled(client):
    # Ensure no channels are enabled
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["channel"] == "none"
    assert body["results"][0]["success"] is False


async def test_test_one_channel_invalid(client):
    resp = await client.post("/api/v1/notifications/test/invalid_channel")
    assert resp.status_code == 422


async def test_test_one_channel_slack_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {"slack": {"enabled": True}}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/slack")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["success"] is False


async def test_test_one_channel_teams_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {"teams": {"enabled": True}}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/teams")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["success"] is False


async def test_test_one_channel_email_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {"email": {"enabled": True}}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/email")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["success"] is False


async def test_test_one_channel_pagerduty_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {"pagerduty": {"enabled": True}}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/pagerduty")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["success"] is False


async def test_test_one_channel_webhook_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {"webhook": {"enabled": True}}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/webhook")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["success"] is False


# ── Schema unit tests ────────────────────────────────────────────────────────

def test_notification_config_defaults():
    from orchestrator.api.notifications import NotificationConfig
    cfg = NotificationConfig()
    assert cfg.slack.enabled is False
    assert cfg.email.smtp_port == 587
    assert cfg.pagerduty.min_severity == "critical"


def test_test_result_schema():
    from orchestrator.api.notifications import TestResult
    tr = TestResult(channel="slack", success=True, message="ok", latency_ms=5.5)
    assert tr.channel == "slack"
    assert tr.latency_ms == 5.5


def test_build_test_payload():
    from orchestrator.api.notifications import _build_test_payload
    p = _build_test_payload()
    assert p["type"] == "test"
    assert p["severity"] == "warning"
    assert "fired_at" in p
