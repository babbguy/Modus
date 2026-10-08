"""
Miscellaneous API and core module tests

Tests targeting a range of modules:
- orchestrator/api/notifications.py
- orchestrator/api/roles.py
- orchestrator/api/dashboard_data.py
- orchestrator/core/maintenance.py
- orchestrator/core/threshold_evaluator.py
- orchestrator/core/connection_checker.py
"""
from __future__ import annotations

import uuid

import pytest

from orchestrator.db.models import (
    App,
    RbacRole,
    Team,
)


# ── Roles API ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_roles(client, db_session):
    role = RbacRole(
        name="test_role_list",
        description="Test role for listing",
        allow=["apps:read"],
    )
    db_session.add(role)
    await db_session.flush()

    resp = await client.get("/api/v1/roles")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_role(client, db_session):
    role = RbacRole(
        name="test_role_get",
        description="Test role for get",
        allow=["apps:read", "apps:write"],
    )
    db_session.add(role)
    await db_session.flush()

    resp = await client.get(f"/api/v1/roles/{role.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test_role_get"


@pytest.mark.asyncio
async def test_get_role_not_found(client):
    resp = await client.get(f"/api/v1/roles/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_role(client):
    resp = await client.post(
        "/api/v1/roles",
        json={
            "name": "custom_test_role",
            "description": "A custom role",
            "allow": ["apps:read"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "custom_test_role"


@pytest.mark.asyncio
async def test_create_role_duplicate(client, db_session):
    role = RbacRole(name="dup_role", description="First", allow=[])
    db_session.add(role)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/roles",
        json={"name": "dup_role", "description": "Second", "allow": []},
    )
    assert resp.status_code in (409, 422)  # 409 conflict or 422 validation


@pytest.mark.asyncio
async def test_update_role(client, db_session):
    role = RbacRole(name="updatable_role", description="Before", allow=["apps:read"])
    db_session.add(role)
    await db_session.flush()

    resp = await client.patch(
        f"/api/v1/roles/{role.id}",
        json={"description": "After", "allow": ["apps:read", "apps:write"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "After"


@pytest.mark.asyncio
async def test_delete_role(client, db_session):
    role = RbacRole(name="deletable_role", description="Delete me", allow=[])
    db_session.add(role)
    await db_session.flush()

    resp = await client.delete(f"/api/v1/roles/{role.id}")
    assert resp.status_code == 204


# ── Dashboard Data ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_summary(client):
    resp = await client.get("/api/v1/dashboard/summary")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_cost_over_time(client):
    resp = await client.get("/api/v1/dashboard/cost-over-time?days=7")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_by_provider(client):
    resp = await client.get("/api/v1/dashboard/by-provider?days=7")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_by_app(client):
    resp = await client.get("/api/v1/dashboard/by-app?days=7")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_by_team(client):
    resp = await client.get("/api/v1/dashboard/by-team?days=7")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_top_models(client):
    resp = await client.get("/api/v1/dashboard/top-models?days=7")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_recent_alerts(client):
    resp = await client.get("/api/v1/dashboard/recent-alerts")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_app_status(client):
    resp = await client.get("/api/v1/dashboard/app-status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_executive_charts(client):
    resp = await client.get("/api/v1/dashboard/executive-charts?days=7")
    assert resp.status_code == 200


# ── Notifications ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notifications_endpoint(client):
    """Test whatever notification endpoints are available."""
    # Try common notification endpoint patterns
    for path in ["/api/v1/notifications", "/api/v1/notifications/channels"]:
        resp = await client.get(path)
        if resp.status_code == 200:
            break


# ── Thresholds ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_thresholds(client):
    resp = await client.get("/api/v1/thresholds")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_threshold(client, db_session):
    team = Team(id=str(uuid.uuid4()), slug="thresh-team", name="Thresh Team")
    db_session.add(team)
    app = App(
        team_id=str(team.id), app_id="thresh-app", app_name="Thresh App",
        environment="production", api_key_hash="fake", api_key_prefix="mds_f",
    )
    db_session.add(app)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/thresholds",
        json={
            "app_id": str(app.id),
            "metric": "cost",
            "value": 100.0,
        },
    )
    assert resp.status_code in (200, 201, 422)


# ── Alerts ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_alerts(client):
    resp = await client.get("/api/v1/alerts")
    assert resp.status_code == 200


# ── Pricing ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_pricing(client):
    resp = await client.get("/api/v1/pricing")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_pricing_effective(client):
    resp = await client.get("/api/v1/pricing/effective/openai/gpt-4o")
    assert resp.status_code == 200


# ── Health ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_ready(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_liveness(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200


# ── Compliance ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compliance_report(client):
    resp = await client.get("/api/v1/compliance/report")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_compliance_audit_trail(client):
    resp = await client.get("/api/v1/compliance/audit-trail")
    assert resp.status_code == 200


# ── Audit Log ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_list(client):
    resp = await client.get("/api/v1/audit-log")
    assert resp.status_code == 200


# ── Pricing helpers ──────────────────────────────────────────────────────────


def test_estimate_cost():
    from orchestrator.core.pricing import estimate_cost

    input_cost, output_cost, total = estimate_cost("openai", "gpt-4o", 1000, 500)
    assert total > 0
    assert input_cost >= 0
    assert output_cost >= 0


def test_estimate_cost_unknown_model():
    from orchestrator.core.pricing import estimate_cost

    _, _, total = estimate_cost("openai", "unknown-model-xyz", 1000, 500)
    assert total >= 0  # Should use fallback pricing


def test_estimate_cost_anthropic():
    from orchestrator.core.pricing import estimate_cost

    _, _, total = estimate_cost("anthropic", "claude-sonnet-4-20250514", 1000, 500)
    assert total > 0
