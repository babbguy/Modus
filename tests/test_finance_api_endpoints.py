"""
Finance API (orchestrator/api/finance.py)

Integration tests for the finance endpoints via test client.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from orchestrator.db.models import (
    BillingConnection,
    CostCenter,
    Team,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

TEAM_ID = str(uuid.uuid4())
TEAM_SLUG = "finance-test"


@pytest_asyncio.fixture
async def seeded_finance(db_session):
    """Seed teams, cost centers, and billing connections for finance tests."""
    team = Team(
        id=TEAM_ID, slug=TEAM_SLUG, name="Finance Test Team",
        budget_monthly_usd=Decimal("10000.00"),
    )
    db_session.add(team)
    await db_session.flush()
    return team


# ── GET /api/v1/finance/summary ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finance_summary(client, seeded_finance):
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_monthly_budget_usd" in data
    assert "total_current_spend_usd" in data
    assert "overall_risk" in data
    assert "cost_center_count" in data
    assert "teams_at_risk" in data
    assert "top_spenders" in data
    assert "spend_by_department" in data


# ── GET /api/v1/finance/burn-rate ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_burn_rate_current(client, seeded_finance):
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert "period" in data
    assert "teams" in data
    assert isinstance(data["teams"], list)
    assert "total_budget_usd" in data


@pytest.mark.asyncio
async def test_burn_rate_prior_month(client, seeded_finance):
    resp = await client.get("/api/v1/finance/burn-rate?period=prior_month")
    assert resp.status_code == 200
    data = resp.json()
    assert "period" in data


# ── Cost Centers CRUD ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_cost_centers_empty(client):
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_cost_center(client):
    resp = await client.post(
        "/api/v1/finance/cost-centers",
        json={
            "name": "Engineering AI",
            "code": "ENG-AI-001",
            "department": "Engineering",
            "division": "Platform",
            "budget_owner_name": "Jane Doe",
            "budget_monthly_usd": "5000.00",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["code"] == "ENG-AI-001"
    assert data["department"] == "Engineering"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_create_cost_center_duplicate_code(client):
    body = {
        "name": "Dup Center",
        "code": "DUP-001",
        "department": "Sales",
    }
    resp1 = await client.post("/api/v1/finance/cost-centers", json=body)
    assert resp1.status_code == 201

    resp2 = await client.post("/api/v1/finance/cost-centers", json=body)
    assert resp2.status_code == 409


# ── Cost Allocation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_allocation_empty(client):
    resp = await client.get("/api/v1/finance/allocation")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_set_allocation(client, db_session):
    team = Team(id=str(uuid.uuid4()), slug="alloc-team", name="Alloc Team")
    db_session.add(team)
    cc = CostCenter(name="Test CC", code="ALLOC-CC-001", department="Eng")
    db_session.add(cc)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/finance/allocation",
        json={
            "team_id": str(team.id),
            "cost_center_id": str(cc.id),
            "allocation_pct": 75.0,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_set_allocation_team_not_found(client):
    resp = await client.post(
        "/api/v1/finance/allocation",
        json={
            "team_id": str(uuid.uuid4()),
            "cost_center_id": str(uuid.uuid4()),
            "allocation_pct": 50.0,
        },
    )
    assert resp.status_code == 404


# ── Chargeback ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chargeback(client):
    resp = await client.get("/api/v1/finance/chargeback")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_chargeback_with_period(client):
    resp = await client.get("/api/v1/finance/chargeback?period=2026-03")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_chargeback_bad_period(client):
    resp = await client.get("/api/v1/finance/chargeback?period=bad")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_generate_chargeback_invoice(client):
    resp = await client.post(
        "/api/v1/finance/chargeback/generate",
        json={"format": "json"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "total_cost_usd" in data
    assert "line_items" in data


# ── What-If Scenarios ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_scenarios_empty(client):
    resp = await client.get("/api/v1/finance/scenarios")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_run_scenario_team_add(client):
    resp = await client.post(
        "/api/v1/finance/scenarios",
        json={
            "name": "Add ML Team",
            "scenario_type": "team_add",
            "parameters": {"num_apps": 10, "avg_cost_per_app_monthly": 200.0},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["scenario_type"] == "team_add"
    assert data["result"] is not None
    assert "projected_monthly_increase_usd" in data["result"]


@pytest.mark.asyncio
async def test_run_scenario_usage_scale(client):
    resp = await client.post(
        "/api/v1/finance/scenarios",
        json={
            "name": "2x Scale",
            "scenario_type": "usage_scale",
            "parameters": {"scale_factor": 2.0},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["result"] is not None


@pytest.mark.asyncio
async def test_run_scenario_model_swap(client):
    resp = await client.post(
        "/api/v1/finance/scenarios",
        json={
            "name": "GPT-4o to GPT-4o-mini",
            "scenario_type": "model_swap",
            "parameters": {
                "from_model": "gpt-4o",
                "to_model": "gpt-4o-mini",
                "cost_ratio": 0.33,
            },
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["result"] is not None


@pytest.mark.asyncio
async def test_run_scenario_budget_change_missing_team(client):
    resp = await client.post(
        "/api/v1/finance/scenarios",
        json={
            "name": "Budget Change",
            "scenario_type": "budget_change",
            "parameters": {"new_budget_monthly_usd": 5000},
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_run_scenario_budget_change(client, seeded_finance):
    resp = await client.post(
        "/api/v1/finance/scenarios",
        json={
            "name": "Budget Change",
            "scenario_type": "budget_change",
            "parameters": {
                "team_id": TEAM_ID,
                "new_budget_monthly_usd": 5000,
            },
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["result"] is not None


# ── Reconciliation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reconciliation_empty(client):
    resp = await client.get("/api/v1/finance/reconciliation")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Audit Export ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_export(client):
    resp = await client.get("/api/v1/finance/audit-export")
    assert resp.status_code == 200
    data = resp.json()
    assert "export_time" in data
    assert "record_count" in data
    assert "records" in data


# ── Billing Connections ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_connections_empty(client):
    resp = await client.get("/api/v1/finance/connections")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_connection(client):
    resp = await client.post(
        "/api/v1/finance/connections",
        json={
            "provider_type": "openai",
            "provider_name": "OpenAI Production",
            "auth_type": "api_key",
            "service_type": "ai",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider_type"] == "openai"
    assert data["provider_name"] == "OpenAI Production"


@pytest.mark.asyncio
async def test_create_connection_invalid_provider(client):
    resp = await client.post(
        "/api/v1/finance/connections",
        json={
            "provider_type": "invalid_provider",
            "provider_name": "Bad",
            "auth_type": "api_key",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_connection_invalid_auth_type(client):
    resp = await client.post(
        "/api/v1/finance/connections",
        json={
            "provider_type": "aws",
            "provider_name": "AWS",
            "auth_type": "invalid_auth",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_connection_invalid_service_type(client):
    resp = await client.post(
        "/api/v1/finance/connections",
        json={
            "provider_type": "aws",
            "provider_name": "AWS",
            "auth_type": "api_key",
            "service_type": "invalid_service",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_connection(client, db_session):
    conn = BillingConnection(
        provider_type="gcp", provider_name="GCP Test",
        auth_type="service_account", status="pending",
        created_by="test",
    )
    db_session.add(conn)
    await db_session.flush()

    resp = await client.delete(f"/api/v1/finance/connections/{conn.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_connection_not_found(client):
    resp = await client.delete(f"/api/v1/finance/connections/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_connection_no_creds(client, db_session):
    conn = BillingConnection(
        provider_type="aws", provider_name="AWS Test",
        auth_type="api_key", status="pending",
        created_by="test",
    )
    db_session.add(conn)
    await db_session.flush()

    resp = await client.post(f"/api/v1/finance/connections/{conn.id}/test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


@pytest.mark.asyncio
async def test_test_connection_with_creds(client, db_session):
    conn = BillingConnection(
        provider_type="openai", provider_name="OpenAI Test",
        auth_type="api_key", status="pending",
        credentials_encrypted="encrypted_data",
        created_by="test",
    )
    db_session.add(conn)
    await db_session.flush()

    resp = await client.post(f"/api/v1/finance/connections/{conn.id}/test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_test_connection_not_found(client):
    resp = await client.post(f"/api/v1/finance/connections/{uuid.uuid4()}/test")
    assert resp.status_code == 404


# ── Spend Trend ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spend_trend_default(client):
    resp = await client.get("/api/v1/finance/spend-trend")
    assert resp.status_code == 200
    data = resp.json()
    assert "days" in data
    assert "series" in data


@pytest.mark.asyncio
async def test_spend_trend_mtd(client):
    resp = await client.get("/api/v1/finance/spend-trend?period=mtd")
    assert resp.status_code == 200
