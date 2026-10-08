"""
Finance API endpoints.

Targets orchestrator.api.finance: burn-rate, cost-centers, allocation,
chargeback, scenarios, reconciliation, audit-export, billing connections,
spend-trend, and summary.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def encryption_key(monkeypatch):
    """Configure a valid MODUS_ENCRYPTION_KEY — credential writes fail closed without one."""
    from cryptography.fernet import Fernet

    from orchestrator.core import credential_crypto
    from orchestrator.core.config import settings

    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    credential_crypto.reset_cipher_cache()
    yield
    credential_crypto.reset_cipher_cache()


async def _create_team(client, slug: str, name: str, budget: str | None = None):
    payload = {"name": name, "slug": slug}
    if budget:
        payload["budget_monthly_usd"] = budget
    resp = await client.post("/api/v1/teams", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. FINANCE SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

async def test_finance_summary_empty(client):
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_monthly_budget_usd" in data
    assert "total_current_spend_usd" in data
    assert "overall_risk" in data
    assert "cost_center_count" in data
    assert isinstance(data["top_spenders"], list)
    assert isinstance(data["spend_by_department"], list)
    assert isinstance(data["recent_invoices"], list)


async def test_finance_summary_with_team(client):
    await _create_team(client, "fin-team-1", "Finance Team 1", "5000.00")
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_monthly_budget_usd"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. BURN RATE
# ═══════════════════════════════════════════════════════════════════════════════

async def test_burn_rate_empty(client):
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert "period" in data
    assert "teams" in data
    assert isinstance(data["teams"], list)
    assert "total_budget_usd" in data
    assert "total_spend_usd" in data
    assert "overall_risk" in data


async def test_burn_rate_with_team(client):
    await _create_team(client, "burn-team-1", "Burn Team 1", "10000.00")
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["teams"], list)


async def test_burn_rate_prior_month(client):
    resp = await client.get("/api/v1/finance/burn-rate?period=prior_month")
    assert resp.status_code == 200
    data = resp.json()
    assert "period" in data


async def test_burn_rate_with_cost_center_filter(client):
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/v1/finance/burn-rate?cost_center_id={fake_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["teams"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. COST CENTERS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_cost_centers_empty(client):
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_cost_center(client):
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Engineering Dept",
        "code": "ENG-001",
        "department": "Engineering",
        "division": "Product",
        "budget_owner_name": "Jane Doe",
        "budget_owner_email": "jane@example.com",
        "budget_monthly_usd": "25000.00",
        "budget_quarterly_usd": "75000.00",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Engineering Dept"
    assert data["code"] == "ENG-001"
    assert data["department"] == "Engineering"
    assert data["is_active"] is True
    assert data["team_count"] == 0
    assert data["budget_monthly_usd"] == "25000.00"
    return data["id"]


async def test_create_cost_center_duplicate_code(client):
    await client.post("/api/v1/finance/cost-centers", json={
        "name": "First CC",
        "code": "DUP-001",
    })
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Second CC",
        "code": "DUP-001",
    })
    assert resp.status_code == 409


async def test_create_cost_center_minimal(client):
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Minimal CC",
        "code": "MIN-001",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["budget_monthly_usd"] is None
    assert data["department"] is None


async def test_list_cost_centers_after_create(client):
    await client.post("/api/v1/finance/cost-centers", json={
        "name": "Listed CC",
        "code": "LIST-001",
        "department": "Sales",
    })
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    centers = resp.json()
    assert len(centers) >= 1
    assert any(c["code"] == "LIST-001" for c in centers)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. COST ALLOCATION
# ═══════════════════════════════════════════════════════════════════════════════

async def test_get_allocation_empty(client):
    resp = await client.get("/api/v1/finance/allocation")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_set_allocation(client):
    team = await _create_team(client, "alloc-team-1", "Alloc Team 1")
    team_id = team["id"]

    cc_resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Alloc CC",
        "code": "ALLOC-001",
    })
    cc_id = cc_resp.json()["id"]

    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team_id,
        "cost_center_id": cc_id,
        "allocation_pct": 100.0,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "ok"
    assert data["team_id"] == team_id


async def test_set_allocation_upsert(client):
    team = await _create_team(client, "alloc-upsert", "Alloc Upsert")
    team_id = team["id"]

    cc_resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Upsert CC",
        "code": "UPSERT-001",
    })
    cc_id = cc_resp.json()["id"]

    # First allocation
    await client.post("/api/v1/finance/allocation", json={
        "team_id": team_id,
        "cost_center_id": cc_id,
        "allocation_pct": 50.0,
    })
    # Upsert: change allocation
    cc2_resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Upsert CC 2",
        "code": "UPSERT-002",
    })
    cc2_id = cc2_resp.json()["id"]

    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team_id,
        "cost_center_id": cc2_id,
        "allocation_pct": 75.0,
    })
    assert resp.status_code == 201


async def test_set_allocation_team_not_found(client):
    cc_resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Ghost CC",
        "code": "GHOST-001",
    })
    cc_id = cc_resp.json()["id"]

    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": str(uuid.uuid4()),
        "cost_center_id": cc_id,
        "allocation_pct": 100.0,
    })
    assert resp.status_code == 404


async def test_set_allocation_cost_center_not_found(client):
    team = await _create_team(client, "alloc-nope", "Alloc Nope")
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": str(uuid.uuid4()),
        "allocation_pct": 100.0,
    })
    assert resp.status_code == 404


async def test_get_allocation_after_set(client):
    team = await _create_team(client, "alloc-read", "Alloc Read")
    cc_resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Read CC",
        "code": "READ-001",
    })
    cc_id = cc_resp.json()["id"]

    await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": cc_id,
        "allocation_pct": 80.0,
    })
    resp = await client.get("/api/v1/finance/allocation")
    assert resp.status_code == 200
    allocs = resp.json()
    assert len(allocs) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CHARGEBACK
# ═══════════════════════════════════════════════════════════════════════════════

async def test_get_chargeback_empty(client):
    resp = await client.get("/api/v1/finance/chargeback")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_chargeback_with_period(client):
    resp = await client.get("/api/v1/finance/chargeback?period=2026-03")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_chargeback_invalid_period(client):
    resp = await client.get("/api/v1/finance/chargeback?period=bad")
    assert resp.status_code == 400


async def test_generate_chargeback_invoice(client):
    resp = await client.post("/api/v1/finance/chargeback/generate", json={
        "format": "json",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "total_cost_usd" in data
    assert data["format"] == "json"
    assert "line_items" in data


async def test_generate_chargeback_csv_format(client):
    resp = await client.post("/api/v1/finance/chargeback/generate", json={
        "format": "csv",
    })
    assert resp.status_code == 201
    assert resp.json()["format"] == "csv"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_scenarios_empty(client):
    resp = await client.get("/api/v1/finance/scenarios")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_run_scenario_model_swap(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Swap GPT-4o to mini",
        "scenario_type": "model_swap",
        "parameters": {
            "from_model": "gpt-4o",
            "to_model": "gpt-4o-mini",
            "cost_ratio": 0.33,
        },
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Swap GPT-4o to mini"
    assert data["scenario_type"] == "model_swap"
    assert "result" in data
    assert data["result"]["from_model"] == "gpt-4o"


async def test_run_scenario_usage_scale(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Double usage",
        "scenario_type": "usage_scale",
        "parameters": {"scale_factor": 2.0},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["result"]["scale_factor"] == 2.0


async def test_run_scenario_team_add(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Add new team",
        "scenario_type": "team_add",
        "parameters": {"num_apps": 10, "avg_cost_per_app_monthly": 250.0},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["result"]["num_apps"] == 10
    assert "projected_monthly_increase_usd" in data["result"]
    assert "projected_annual_increase_usd" in data["result"]


async def test_run_scenario_budget_change(client):
    team = await _create_team(client, "scenario-team", "Scenario Team")
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Budget cut",
        "scenario_type": "budget_change",
        "parameters": {
            "team_id": team["id"],
            "new_budget_monthly_usd": 5000,
        },
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "risk" in data["result"]


async def test_run_scenario_budget_change_no_team_id(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Bad budget change",
        "scenario_type": "budget_change",
        "parameters": {"new_budget_monthly_usd": 1000},
    })
    assert resp.status_code == 400


async def test_list_scenarios_after_create(client):
    await client.post("/api/v1/finance/scenarios", json={
        "name": "For listing",
        "scenario_type": "team_add",
        "parameters": {"num_apps": 3, "avg_cost_per_app_monthly": 100.0},
    })
    resp = await client.get("/api/v1/finance/scenarios")
    assert resp.status_code == 200
    scenarios = resp.json()
    assert len(scenarios) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 7. RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════════════

async def test_reconciliation_empty(client):
    resp = await client.get("/api/v1/finance/reconciliation")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_reconciliation_with_provider_filter(client):
    resp = await client.get("/api/v1/finance/reconciliation?provider=openai")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. AUDIT EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

async def test_audit_export_empty(client):
    resp = await client.get("/api/v1/finance/audit-export")
    assert resp.status_code == 200
    data = resp.json()
    assert "export_time" in data
    assert "record_count" in data
    assert data["record_count"] == 0
    assert isinstance(data["records"], list)


async def test_audit_export_with_filters(client):
    resp = await client.get(
        "/api/v1/finance/audit-export?"
        "action=policy.create&"
        f"team_id={uuid.uuid4()}"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filters"]["action"] == "policy.create"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. BILLING CONNECTIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_connections_empty(client):
    resp = await client.get("/api/v1/finance/connections")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_connection(client, encryption_key):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "openai",
        "provider_name": "OpenAI Production",
        "service_type": "ai_provider",
        "auth_type": "api_key",
        "credentials": {"api_key": "sk-test123"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider_type"] == "openai"
    assert data["provider_name"] == "OpenAI Production"
    assert data["has_credentials"] is True
    return data["id"]


async def test_create_connection_invalid_provider(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "invalid_provider",
        "provider_name": "Bad",
        "auth_type": "api_key",
    })
    assert resp.status_code == 400


async def test_create_connection_invalid_auth_type(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "openai",
        "provider_name": "Bad Auth",
        "auth_type": "invalid_auth",
    })
    assert resp.status_code == 400


async def test_create_connection_invalid_service_type(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "openai",
        "provider_name": "Bad Service",
        "auth_type": "api_key",
        "service_type": "invalid_service",
    })
    assert resp.status_code == 400


async def test_list_connections_after_create(client):
    await client.post("/api/v1/finance/connections", json={
        "provider_type": "anthropic",
        "provider_name": "Anthropic Prod",
        "auth_type": "api_key",
    })
    resp = await client.get("/api/v1/finance/connections")
    assert resp.status_code == 200
    conns = resp.json()
    assert len(conns) >= 1


async def test_delete_connection(client):
    create_resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "gcp",
        "provider_name": "GCP Staging",
        "auth_type": "service_account",
    })
    conn_id = create_resp.json()["id"]
    resp = await client.delete(f"/api/v1/finance/connections/{conn_id}")
    assert resp.status_code == 204


async def test_delete_connection_not_found(client):
    resp = await client.delete(f"/api/v1/finance/connections/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_test_connection_with_credentials(client, encryption_key):
    create_resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "openai",
        "provider_name": "Test Conn",
        "auth_type": "api_key",
        "credentials": {"api_key": "sk-test-key"},
    })
    conn_id = create_resp.json()["id"]
    resp = await client.post(f"/api/v1/finance/connections/{conn_id}/test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_test_connection_without_credentials(client):
    create_resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "aws",
        "provider_name": "No Creds",
        "auth_type": "iam_role",
    })
    conn_id = create_resp.json()["id"]
    resp = await client.post(f"/api/v1/finance/connections/{conn_id}/test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


async def test_test_connection_not_found(client):
    resp = await client.post(f"/api/v1/finance/connections/{uuid.uuid4()}/test")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 10. SPEND TREND
# ═══════════════════════════════════════════════════════════════════════════════

async def test_spend_trend_empty(client):
    resp = await client.get("/api/v1/finance/spend-trend")
    assert resp.status_code == 200
    data = resp.json()
    assert "days" in data
    assert "series" in data
    assert isinstance(data["days"], list)
    assert isinstance(data["series"], list)


async def test_spend_trend_mtd(client):
    resp = await client.get("/api/v1/finance/spend-trend?period=mtd")
    assert resp.status_code == 200


async def test_spend_trend_with_filters(client):
    resp = await client.get(
        "/api/v1/finance/spend-trend?days=7&provider=openai"
    )
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 11. HELPER FUNCTIONS (unit tests)
# ═══════════════════════════════════════════════════════════════════════════════

def test_classify_risk():
    from orchestrator.api.finance import _classify_risk
    assert _classify_risk(Decimal("50")) == "on-track"
    assert _classify_risk(Decimal("79.9")) == "on-track"
    assert _classify_risk(Decimal("80")) == "at-risk"
    assert _classify_risk(Decimal("99.9")) == "at-risk"
    assert _classify_risk(Decimal("100")) == "over-budget"
    assert _classify_risk(Decimal("150")) == "over-budget"


def test_project_eom():
    from orchestrator.api.finance import _project_eom
    # Zero days elapsed returns current spend
    result = _project_eom(Decimal("100"), 0, 30)
    assert result == Decimal("100")

    # Normal projection
    result = _project_eom(Decimal("100"), 10, 30)
    assert result == Decimal("300.00")

    # Full month elapsed
    result = _project_eom(Decimal("300"), 30, 30)
    assert result == Decimal("300.00")
