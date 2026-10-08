"""
Smoke tests for dashboard gap fixes (production readiness rework).

Covers:
- GAP-10: GET /api/v1/audit-log endpoint
- GAP-7:  GET /api/v1/finance/burn-rate?period=prior_month parameter
- GAP-8:  AlertSummary now exposes notification_sent / notification_result fields
- GAP-3:  GET /api/v1/policy/decisions endpoint reachable

These are *contract* tests — they verify the routes exist and return the
expected shape. They do not require seeded data.
"""
from __future__ import annotations

# ── GAP-10: audit-log endpoint ──────────────────────────────────────────────

async def test_audit_log_endpoint_exists(client):
    """GAP-10 fix: /api/v1/audit-log must be reachable and return a list."""
    resp = await client.get("/api/v1/audit-log")
    assert resp.status_code == 200, f"audit-log endpoint missing: {resp.status_code}"
    data = resp.json()
    assert isinstance(data, list)


async def test_audit_log_resource_type_filter(client):
    """GAP-10 fix: filter by resource_type=pricing_override (used by Pricing view)."""
    resp = await client.get("/api/v1/audit-log?resource_type=pricing_override&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # All returned entries should match the filter (when there is data)
    for entry in data:
        assert entry["resource_type"] == "pricing_override"


async def test_audit_log_limit_param(client):
    """GAP-10 fix: limit parameter is honored."""
    resp = await client.get("/api/v1/audit-log?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) <= 5


async def test_audit_log_invalid_limit_rejected(client):
    """Limit validation: > 500 should be rejected."""
    resp = await client.get("/api/v1/audit-log?limit=10000")
    assert resp.status_code == 422


# ── GAP-7: burn-rate prior_month parameter ──────────────────────────────────

async def test_burn_rate_default_period(client):
    """Burn-rate without period param returns current month-to-date."""
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert "teams" in data
    assert "period" in data


async def test_burn_rate_prior_month_period(client):
    """GAP-7 fix: period=prior_month returns the previous calendar month."""
    resp_current = await client.get("/api/v1/finance/burn-rate?period=current")
    resp_prior = await client.get("/api/v1/finance/burn-rate?period=prior_month")
    assert resp_current.status_code == 200
    assert resp_prior.status_code == 200
    cur = resp_current.json()
    prior = resp_prior.json()
    # The "period" field encodes YYYY-MM and must differ between current and prior
    assert cur["period"] != prior["period"], (
        f"compare mode bug: current={cur['period']} prior={prior['period']} "
        "— period parameter has no effect"
    )


# ── GAP-8: AlertSummary delivery fields ─────────────────────────────────────

async def test_recent_alerts_includes_notification_fields(client):
    """GAP-8 fix: recent-alerts response shape includes notification_sent
    and notification_result keys (the dashboard delivery tile reads these)."""
    resp = await client.get("/api/v1/dashboard/recent-alerts?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # If any alerts exist, every record must include the new fields
    for alert in data:
        assert "notification_sent" in alert, (
            "GAP-8 regression: notification_sent missing from AlertSummary"
        )
        assert "notification_result" in alert, (
            "GAP-8 regression: notification_result missing from AlertSummary"
        )


# ── GAP-3: policy decisions endpoint ────────────────────────────────────────

async def test_policy_decisions_endpoint_exists(client):
    """GAP-3 fix: the dashboard Enforcement Detail tile calls this endpoint."""
    resp = await client.get("/api/v1/policy/decisions?limit=10")
    # 200 (data or empty) or 404 if not registered — must be 200
    assert resp.status_code == 200, (
        f"GAP-3: /api/v1/policy/decisions not reachable ({resp.status_code})"
    )
    data = resp.json()
    # Backend may return list directly or wrap in {decisions: [...]} or {items: [...]}
    if isinstance(data, dict):
        assert any(k in data for k in ("decisions", "items"))
    else:
        assert isinstance(data, list)
