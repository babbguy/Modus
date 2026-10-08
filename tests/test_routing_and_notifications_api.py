"""
API Endpoints (routing, notifications)

Tests:
  - Routing API: fingerprints list/get/reset/exclude/patch, outcomes,
    savings, summary, savings-over-time, outcome batch
  - Notifications API: config get/put, test channels, build_test_payload
"""
from __future__ import annotations

import uuid

# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def _seed_fingerprint(client, app, fp_hash="fp-abc123"):
    """Seed a fingerprint via the routing outcome endpoint as ``app``."""
    resp = await client.post("/api/v1/routing/outcomes", json={
        "fingerprint_hash": fp_hash,
        "routed_to": "cheap",
        "system_prompt_hash": "sp-hash-123",
        "cheap_model": "claude-haiku-4-5-20251001",
        "expensive_model": "claude-sonnet-4-5-20250929",
        "cost_saved": 0.05,
    }, headers={"X-Modus-APIKey": app["api_key"]})
    return resp


# ═════════════════════════════════════════════════════════════════════════════
# 1. ROUTING — FINGERPRINTS
# ═════════════════════════════════════════════════════════════════════════════

async def test_routing_fingerprints_empty(client):
    resp = await client.get("/api/v1/routing/fingerprints")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_routing_log_outcome_creates_fingerprint(client, registered_app):
    resp = await _seed_fingerprint(client, registered_app, fp_hash="fp-new-1")
    assert resp.status_code == 201
    assert resp.json()["status"] == "logged"


async def test_routing_fingerprint_get(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-get-1")
    resp = await client.get("/api/v1/routing/fingerprints/fp-get-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["fingerprint_hash"] == "fp-get-1"
    assert data["phase"] == "observe"


async def test_routing_fingerprint_not_found(client):
    resp = await client.get("/api/v1/routing/fingerprints/does-not-exist")
    assert resp.status_code == 404


async def test_routing_fingerprint_reset(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-reset-1")
    resp = await client.post("/api/v1/routing/fingerprints/fp-reset-1/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reset_to_observe"


async def test_routing_fingerprint_reset_not_found(client):
    resp = await client.post("/api/v1/routing/fingerprints/nope/reset")
    assert resp.status_code == 404


async def test_routing_fingerprint_exclude(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-excl-1")
    resp = await client.post("/api/v1/routing/fingerprints/fp-excl-1/exclude")
    assert resp.status_code == 200
    assert resp.json()["status"] == "excluded"

    # Verify it's excluded
    get_resp = await client.get("/api/v1/routing/fingerprints/fp-excl-1")
    assert get_resp.json()["phase"] == "excluded"
    assert get_resp.json()["allow_routing"] is False


async def test_routing_fingerprint_exclude_not_found(client):
    resp = await client.post("/api/v1/routing/fingerprints/nope/exclude")
    assert resp.status_code == 404


async def test_routing_fingerprint_patch(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-patch-1")
    resp = await client.patch("/api/v1/routing/fingerprints/fp-patch-1", json={
        "allow_routing": False,
        "force_model": "gpt-3.5-turbo",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["allow_routing"] is False
    assert data["force_model"] == "gpt-3.5-turbo"


async def test_routing_fingerprint_patch_not_found(client):
    resp = await client.patch("/api/v1/routing/fingerprints/nope", json={
        "allow_routing": True,
    })
    assert resp.status_code == 404


async def test_routing_fingerprint_patch_clear_force_model(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-patch-2")
    # Set force_model
    await client.patch("/api/v1/routing/fingerprints/fp-patch-2", json={
        "force_model": "gpt-4",
    })
    # Clear force_model with empty string
    resp = await client.patch("/api/v1/routing/fingerprints/fp-patch-2", json={
        "force_model": "",
    })
    assert resp.status_code == 200
    assert resp.json()["force_model"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 2. ROUTING — OUTCOMES
# ═════════════════════════════════════════════════════════════════════════════

async def test_routing_outcomes_empty(client):
    resp = await client.get("/api/v1/routing/outcomes")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_routing_outcomes_with_data(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-out-1")
    resp = await client.get("/api/v1/routing/outcomes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


async def test_routing_outcomes_filter(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-filter-1")
    resp = await client.get("/api/v1/routing/outcomes", params={
        "fingerprint_hash": "fp-filter-1",
    })
    assert resp.status_code == 200


async def test_routing_outcome_batch(client, registered_app):
    app_id = str(uuid.uuid4())
    resp = await client.post("/api/v1/routing/outcomes/batch", json={
        "outcomes": [
            {
                "fingerprint_hash": "fp-batch-1",
                "app_id": app_id,
                "routed_to": "cheap",
                "system_prompt_hash": "sp1",
                "cost_saved": 0.02,
            },
            {
                "fingerprint_hash": "fp-batch-2",
                "app_id": app_id,
                "routed_to": "expensive",
                "system_prompt_hash": "sp2",
            },
        ],
    }, headers={"X-Modus-APIKey": registered_app["api_key"]})
    assert resp.status_code == 201
    data = resp.json()
    assert data["accepted"] == 2
    assert data["errors"] == 0


async def test_routing_outcome_batch_with_errors(client, registered_app):
    resp = await client.post("/api/v1/routing/outcomes/batch", json={
        "outcomes": [
            {"fingerprint_hash": "", "app_id": "", "routed_to": "cheap"},
            {"fingerprint_hash": "valid-fp", "app_id": str(uuid.uuid4()), "routed_to": "cheap"},
        ],
    }, headers={"X-Modus-APIKey": registered_app["api_key"]})
    assert resp.status_code == 201
    data = resp.json()
    assert data["errors"] == 1
    assert data["accepted"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 3. ROUTING — SAVINGS / SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

async def test_routing_savings(client):
    resp = await client.get("/api/v1/routing/savings")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_cost_saved" in data
    assert "active_fingerprints" in data


async def test_routing_summary(client):
    resp = await client.get("/api/v1/routing/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_fingerprints" in data
    assert "escalation_rate" in data
    assert "cost_saved_30d_usd" in data


async def test_routing_summary_with_app_filter(client):
    resp = await client.get("/api/v1/routing/summary", params={"app_id": str(uuid.uuid4())})
    assert resp.status_code == 200


async def test_routing_savings_over_time(client):
    resp = await client.get("/api/v1/routing/savings-over-time")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_routing_savings_over_time_with_params(client):
    resp = await client.get("/api/v1/routing/savings-over-time", params={
        "days": 7,
        "app_id": str(uuid.uuid4()),
    })
    assert resp.status_code == 200


async def test_routing_fingerprints_list_with_filters(client, registered_app):
    await _seed_fingerprint(client, registered_app, fp_hash="fp-list-1")
    resp = await client.get("/api/v1/routing/fingerprints", params={
        "phase": "observe",
        "limit": 10,
        "offset": 0,
    })
    assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
# 4. NOTIFICATIONS — CONFIG
# ═════════════════════════════════════════════════════════════════════════════

async def test_notifications_get_config(client):
    resp = await client.get("/api/v1/notifications/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "slack" in data
    assert "email" in data
    assert "teams" in data
    assert "pagerduty" in data
    assert "webhook" in data


async def test_notifications_save_config(client):
    resp = await client.put("/api/v1/notifications/config", json={
        "slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/test"},
        "teams": {"enabled": False},
        "email": {"enabled": False},
        "pagerduty": {"enabled": False},
        "webhook": {"enabled": False},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["slack"]["enabled"] is True


async def test_notifications_save_config_with_secrets(client):
    """Verify secrets are accepted in save."""
    resp = await client.put("/api/v1/notifications/config", json={
        "slack": {"enabled": False},
        "teams": {"enabled": False},
        "email": {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_password": "supersecret",
            "recipients": ["admin@example.com"],
        },
        "pagerduty": {
            "enabled": True,
            "integration_key": "pd-key-123",
        },
        "webhook": {
            "enabled": True,
            "url": "https://example.com/hook",
            "secret_value": "webhook-secret",
        },
    })
    assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
# 5. NOTIFICATIONS — TEST CHANNELS
# ═════════════════════════════════════════════════════════════════════════════

async def test_notifications_test_no_channels(client):
    # Reset cache to empty config
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["channel"] == "none"
    assert data["results"][0]["success"] is False


async def test_notifications_test_unknown_channel(client):
    resp = await client.post("/api/v1/notifications/test/foobar")
    assert resp.status_code == 422


async def test_notifications_test_slack_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/slack")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["success"] is False


async def test_notifications_test_email_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/email")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False


async def test_notifications_test_teams_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False


async def test_notifications_test_pagerduty_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/pagerduty")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False


async def test_notifications_test_webhook_unconfigured(client):
    import orchestrator.api.notifications as notif_mod
    notif_mod._config_cache = {}
    notif_mod._cache_loaded = True

    resp = await client.post("/api/v1/notifications/test/webhook")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["success"] is False


# ═════════════════════════════════════════════════════════════════════════════
# 6. NOTIFICATIONS — BUILD TEST PAYLOAD
# ═════════════════════════════════════════════════════════════════════════════

async def test_build_test_payload():
    from orchestrator.api.notifications import _build_test_payload
    payload = _build_test_payload()
    assert payload["type"] == "test"
    assert payload["severity"] == "warning"
    assert "fired_at" in payload
    assert "dashboard_url" in payload
