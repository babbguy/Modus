"""Tests for orchestrator.api.notifications — notification config and test-fire."""
from __future__ import annotations

import pytest

# Reset notification cache before each test module run
import orchestrator.api.notifications as notif_mod

notif_mod._config_cache = {}
notif_mod._cache_loaded = False


# ── GET /notifications/config ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_config_default(client):
    resp = await client.get("/api/v1/notifications/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "slack" in data
    assert "teams" in data
    assert "email" in data
    assert "pagerduty" in data
    assert "webhook" in data
    assert data["slack"]["enabled"] is False


# ── PUT /notifications/config ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_config(client):
    resp = await client.put("/api/v1/notifications/config", json={
        "slack": {
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/test",
            "channel": "#alerts",
            "min_severity": "critical",
        },
        "teams": {"enabled": False},
        "email": {"enabled": False},
        "pagerduty": {"enabled": False},
        "webhook": {"enabled": False},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["slack"]["enabled"] is True
    assert data["slack"]["channel"] == "#alerts"


@pytest.mark.asyncio
async def test_save_config_roundtrip(client):
    """Save and immediately read back."""
    cfg = {
        "slack": {"enabled": False},
        "teams": {"enabled": True, "webhook_url": "https://teams.example.com"},
        "email": {"enabled": False},
        "pagerduty": {"enabled": False},
        "webhook": {"enabled": True, "url": "https://webhook.example.com"},
    }
    await client.put("/api/v1/notifications/config", json=cfg)

    # Reset cache to force DB read
    notif_mod._cache_loaded = False

    resp = await client.get("/api/v1/notifications/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["teams"]["enabled"] is True
    assert data["webhook"]["enabled"] is True


# ── POST /notifications/test ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_all_channels_none_enabled(client):
    """When no channels are configured, returns 'none' result."""
    # Reset cache
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 1
    assert data["results"][0]["channel"] == "none"
    assert data["results"][0]["success"] is False


# ── POST /notifications/test/{channel} ───────────────────────────────────────


@pytest.mark.asyncio
async def test_test_one_channel_invalid(client):
    resp = await client.post("/api/v1/notifications/test/fax")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_test_one_channel_slack_not_configured(client):
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/slack")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False
    assert "not configured" in data["results"][0]["message"].lower()


@pytest.mark.asyncio
async def test_test_one_channel_teams_not_configured(client):
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False


@pytest.mark.asyncio
async def test_test_one_channel_email_not_configured(client):
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/email")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False


@pytest.mark.asyncio
async def test_test_one_channel_pagerduty_not_configured(client):
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/pagerduty")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False


@pytest.mark.asyncio
async def test_test_one_channel_webhook_not_configured(client):
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/webhook")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False
