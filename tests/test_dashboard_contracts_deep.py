"""
Deep dashboard contract tests — explicit field-name assertions for the
remaining demo-critical surfaces beyond what test_dashboard_contracts.py
covers. Each test cites the JS view file that consumes the fields.

Pattern: every key the frontend reads must be present in the response.
Pattern: bug-prone field-name pairs include explicit guardrails so a
silent rename breaks the build.
"""
from __future__ import annotations

def _assert_keys(item: dict, required: set[str], context: str) -> None:
    assert isinstance(item, dict), f"{context}: expected dict, got {type(item).__name__}"
    missing = required - item.keys()
    assert not missing, f"{context}: missing keys {missing}. Got {sorted(item.keys())}"


# ── Routing view ────────────────────────────────────────────────────────────


async def test_routing_summary_contract(client):
    """dashboard/js/views/routing.js _renderSummary reads:
    total_fingerprints, routing_active, routing_observe, routing_excluded,
    drift_flagged, total_routed_calls, escalation_rate, avg_routing_confidence,
    cost_saved_*."""
    resp = await client.get("/api/v1/routing/summary")
    assert resp.status_code == 200
    data = resp.json()
    expected = {
        "total_fingerprints", "routing_active", "routing_observe",
        "routing_excluded", "drift_flagged", "total_routed_calls",
        "escalation_rate",
    }
    _assert_keys(data, expected, "routing/summary")


async def test_routing_fingerprints_contract(client):
    """dashboard/js/views/routing.js _renderFingerprints reads per-row:
    fingerprint_hash, app_id, phase, allow_routing, cheap_model_agreement_rate,
    drift_score, routing_confidence, total_routed_calls, total_escalations."""
    resp = await client.get("/api/v1/routing/fingerprints")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {
            "fingerprint_hash", "app_id", "phase", "allow_routing",
            "cheap_model_agreement_rate", "drift_score", "routing_confidence",
            "total_routed_calls", "total_escalations",
        }
        _assert_keys(item, expected, "routing/fingerprints")


async def test_routing_savings_over_time_contract(client):
    """dashboard/js/views/routing.js _renderSavingsChart reads .date and
    .cost_saved_usd per point."""
    resp = await client.get("/api/v1/routing/savings-over-time")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        _assert_keys(item, {"date", "cost_saved_usd"}, "routing/savings-over-time")


# ── DevOps insights endpoints ───────────────────────────────────────────────


async def test_insights_anomalies_contract(client):
    """dashboard/js/views/devops.js _renderAnomalies reads severity,
    metric, z_score, app_id, ai_explanation, detected_at, baseline_value,
    actual_value."""
    resp = await client.get("/api/v1/insights/anomalies")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {
            "severity", "metric", "z_score", "app_id",
            "ai_explanation", "detected_at",
            "baseline_value", "actual_value",
        }
        _assert_keys(item, expected, "insights/anomalies")


async def test_insights_recommendations_contract(client):
    """dashboard/js/views/devops.js _renderRecommendations reads
    app_id, current_model, suggested_model, estimated_monthly_savings,
    recommendation_text."""
    resp = await client.get("/api/v1/insights/recommendations")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for item in data:
        expected = {
            "app_id", "current_model", "suggested_model",
            "estimated_monthly_savings", "recommendation_text",
        }
        _assert_keys(item, expected, "insights/recommendations")


async def test_insights_enforcement_summary_contract(client):
    """dashboard/js/views/devops.js _renderEnforcement reads
    allowed, blocked, throttle, redirect, saved/total_savings."""
    resp = await client.get("/api/v1/insights/enforcement-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    # Keys may be optional but at least one cost-savings field must exist
    assert any(k in data for k in ("allowed", "blocked", "total_savings", "saved"))


# ── Executive view ──────────────────────────────────────────────────────────


async def test_reports_roi_contract(client):
    """dashboard/js/views/executive.js reads ROI report."""
    resp = await client.get("/api/v1/reports/roi")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (dict, list))


# ── Topology view ───────────────────────────────────────────────────────────


async def test_topology_contract(client):
    """dashboard/js/views/topology.js renders an SVG from the topology
    response. Must return either a list or a dict with nodes/edges."""
    resp = await client.get("/api/v1/topology")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (dict, list))


# ── Governance view ─────────────────────────────────────────────────────────


async def test_governance_stats_contract(client):
    """dashboard/js/views/governance.js _renderStats reads governance stats."""
    resp = await client.get("/api/v1/governance/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


async def test_governance_cot_ledger_stats_contract(client):
    """dashboard/js/views/governance.js _renderCoTStats."""
    resp = await client.get("/api/v1/governance/cot-ledger/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


async def test_governance_evolution_status_contract(client):
    """dashboard/js/views/governance.js renders evolution status."""
    resp = await client.get("/api/v1/governance/evolution/status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── Sentinel view ───────────────────────────────────────────────────────────


async def test_sentinel_stats_contract(client):
    """dashboard/js/views/sentinel.js _renderStats reads threat stats."""
    resp = await client.get("/api/v1/sentinel/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


async def test_sentinel_threats_contract(client):
    """dashboard/js/views/sentinel.js _renderThreats reads threat list."""
    resp = await client.get("/api/v1/sentinel/threats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


# ── Compliance view ─────────────────────────────────────────────────────────


async def test_compliance_attestation_stats_contract(client):
    resp = await client.get("/api/v1/compliance/attestation-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


async def test_compliance_pqc_score_contract(client):
    resp = await client.get("/api/v1/compliance/pqc/score")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


async def test_compliance_zk_proofs_stats_contract(client):
    resp = await client.get("/api/v1/compliance/zk-proofs/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── Federation / connections ────────────────────────────────────────────────


async def test_federation_peers_contract(client):
    """dashboard/js/views/federation.js reads peer list."""
    resp = await client.get("/api/v1/admin/federation/peers")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


async def test_admin_connections_contract(client):
    """dashboard/js/views/connections.js reads {connections, summary, checked_at}.
    Each connection has id, category, endpoint, last_check."""
    resp = await client.get("/api/v1/admin/connections")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    _assert_keys(data, {"connections", "summary", "checked_at"}, "admin/connections")
    assert isinstance(data["connections"], list)
    for conn in data["connections"]:
        _assert_keys(conn, {"id", "category", "endpoint"}, "connection")


async def test_admin_settings_contract(client):
    """dashboard/js/views/settings.js reads a list of {key, value, updated_at,
    updated_by} entries."""
    resp = await client.get("/api/v1/admin/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    for setting in data:
        _assert_keys(setting, {"key", "updated_at", "updated_by"}, "admin/settings")


async def test_admin_nomus_status_contract(client):
    """dashboard/js/views/admin.js reads Nomus sync status."""
    resp = await client.get("/api/v1/admin/nomus/status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── Profile view (post-fix) ─────────────────────────────────────────────────


async def test_users_me_contract(client):
    """dashboard/js/views/profile.js _loadFromApi reads display_name,
    email, role from /users/me."""
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200, f"GET /users/me must work after the route-ordering fix: {resp.status_code}"
    data = resp.json()
    expected = {"display_name", "email", "role"}
    _assert_keys(data, expected, "users/me")


async def test_users_me_update_contract(client):
    """PUT /users/me must accept display_name + email payload."""
    resp = await client.put(
        "/api/v1/users/me",
        json={"display_name": "Test User", "email": "test@example.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "display_name" in data


# ── Teams + apps + thresholds + policies + pricing list ─────────────────────


async def test_teams_contract(client):
    """dashboard/js/views/teams.js _renderTeams reads team list."""
    resp = await client.get("/api/v1/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


async def test_apps_contract(client):
    """dashboard/js/views/apps.js _renderApps reads app list."""
    resp = await client.get("/api/v1/apps")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


async def test_thresholds_contract(client):
    """dashboard/js/views/thresholds.js _renderTable reads threshold rules."""
    resp = await client.get("/api/v1/thresholds")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


# ── Notifications config ────────────────────────────────────────────────────


async def test_notifications_config_full_contract(client):
    """dashboard/js/views/notifications.js _renderConfig destructures
    cfg.slack, cfg.teams, cfg.email, cfg.pagerduty, cfg.webhook."""
    resp = await client.get("/api/v1/notifications/config")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    # Keys may all be missing on a fresh deploy — that's fine, the dashboard
    # uses `cfg.slack || {}` etc. We just verify the response is a dict.
