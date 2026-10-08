"""
Finance API deeper coverage.

Targets orchestrator.api.finance: spend-trend, forecast, scenario execution,
billing connections, reconciliation, chargeback generation, summary with data.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _create_team(client, slug: str, name: str, budget: str | None = None):
    payload = {"name": name, "slug": slug}
    if budget:
        payload["budget_monthly_usd"] = budget
    resp = await client.post("/api/v1/teams", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _create_cost_center(client, name: str, code: str, dept: str = "Engineering",
                               budget: str | None = None):
    payload = {
        "name": name,
        "code": code,
        "department": dept,
    }
    if budget:
        payload["budget_monthly_usd"] = budget
    resp = await client.post("/api/v1/finance/cost-centers", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. RISK CLASSIFICATION HELPER
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
    # If 0 days elapsed, return current spend as-is
    result = _project_eom(Decimal("100"), 0, 30)
    assert result == Decimal("100")
    # Normal projection: $100 in 10 days -> $300 in 30 days
    result = _project_eom(Decimal("100"), 10, 30)
    assert result == Decimal("300.00")
    # Mid-month
    result = _project_eom(Decimal("500"), 15, 30)
    assert result == Decimal("1000.00")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. COST CENTERS CRUD
# ═══════════════════════════════════════════════════════════════════════════════

async def test_create_cost_center(client):
    data = await _create_cost_center(client, "Engineering", "ENG-001", "Engineering", "10000.00")
    assert data["code"] == "ENG-001"
    assert data["department"] == "Engineering"
    assert data["is_active"] is True
    assert data["team_count"] == 0
    assert data["current_spend_usd"] == "0.00"


async def test_create_duplicate_cost_center(client):
    await _create_cost_center(client, "Marketing", "MKT-001")
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Marketing 2", "code": "MKT-001", "department": "Marketing",
    })
    assert resp.status_code == 409


async def test_list_cost_centers(client):
    await _create_cost_center(client, "Sales", "SAL-001", "Sales")
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(cc["code"] == "SAL-001" for cc in data)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ALLOCATION
# ═══════════════════════════════════════════════════════════════════════════════

async def test_get_allocation_empty(client):
    resp = await client.get("/api/v1/finance/allocation")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_set_allocation(client):
    team = await _create_team(client, "alloc-team", "Alloc Team")
    cc = await _create_cost_center(client, "Alloc CC", "ALLOC-001")
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": cc["id"],
        "allocation_pct": 100.0,
    })
    assert resp.status_code == 201
    assert resp.json()["status"] == "ok"


async def test_set_allocation_invalid_team(client):
    cc = await _create_cost_center(client, "Alloc CC2", "ALLOC-002")
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": str(uuid.uuid4()),
        "cost_center_id": cc["id"],
        "allocation_pct": 50.0,
    })
    assert resp.status_code == 404


async def test_set_allocation_invalid_cost_center(client):
    team = await _create_team(client, "alloc-team-2", "Alloc Team 2")
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": str(uuid.uuid4()),
        "allocation_pct": 50.0,
    })
    assert resp.status_code == 404


async def test_upsert_allocation(client):
    team = await _create_team(client, "alloc-upsert", "Alloc Upsert")
    cc1 = await _create_cost_center(client, "CC Upsert 1", "UPS-001")
    cc2 = await _create_cost_center(client, "CC Upsert 2", "UPS-002")

    # First allocation
    resp1 = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"], "cost_center_id": cc1["id"], "allocation_pct": 100.0,
    })
    assert resp1.status_code == 201

    # Upsert to different CC
    resp2 = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"], "cost_center_id": cc2["id"], "allocation_pct": 75.0,
    })
    assert resp2.status_code == 201
    assert resp2.json()["cost_center_id"] == cc2["id"]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CHARGEBACK
# ═══════════════════════════════════════════════════════════════════════════════

async def test_chargeback_empty(client):
    resp = await client.get("/api/v1/finance/chargeback")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_chargeback_with_period(client):
    resp = await client.get("/api/v1/finance/chargeback?period=2026-03")
    assert resp.status_code == 200


async def test_chargeback_bad_period(client):
    resp = await client.get("/api/v1/finance/chargeback?period=bad")
    assert resp.status_code == 400


async def test_generate_chargeback_invoice(client):
    resp = await client.post("/api/v1/finance/chargeback/generate", json={
        "format": "json",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "period_start" in data
    assert "period_end" in data
    assert "total_cost_usd" in data
    assert "line_items" in data


async def test_generate_chargeback_csv_format(client):
    resp = await client.post("/api/v1/finance/chargeback/generate", json={
        "format": "csv",
    })
    assert resp.status_code == 201
    assert resp.json()["format"] == "csv"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_scenarios_empty(client):
    resp = await client.get("/api/v1/finance/scenarios")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_run_scenario_model_swap(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Test Model Swap",
        "scenario_type": "model_swap",
        "parameters": {"from_model": "gpt-4o", "to_model": "gpt-4o-mini"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["scenario_type"] == "model_swap"
    assert data["result"] is not None


async def test_run_scenario_usage_scale(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Test Scale",
        "scenario_type": "usage_scale",
        "parameters": {"scale_factor": 2.0},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["scenario_type"] == "usage_scale"


async def test_run_scenario_team_add(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Add Team",
        "scenario_type": "team_add",
        "parameters": {"num_apps": 5, "avg_cost_per_app_monthly": 200},
    })
    assert resp.status_code == 201


async def test_run_scenario_budget_change(client):
    team = await _create_team(client, "sc-team", "Scenario Team", "5000.00")
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Budget Change",
        "scenario_type": "budget_change",
        "parameters": {"team_id": team["id"], "new_budget_monthly_usd": "8000.00"},
    })
    assert resp.status_code == 201


# ═══════════════════════════════════════════════════════════════════════════════
# 6. BURN RATE
# ═══════════════════════════════════════════════════════════════════════════════

async def test_burn_rate_with_cost_center(client):
    team = await _create_team(client, "br-team", "BR Team", "10000.00")
    cc = await _create_cost_center(client, "BR CC", "BR-001")
    await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"], "cost_center_id": cc["id"], "allocation_pct": 100.0,
    })
    resp = await client.get(f"/api/v1/finance/burn-rate?cost_center_id={cc['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert "teams" in data


async def test_burn_rate_prior_month(client):
    resp = await client.get("/api/v1/finance/burn-rate?period=prior_month")
    assert resp.status_code == 200
    data = resp.json()
    assert "period" in data


# ═══════════════════════════════════════════════════════════════════════════════
# 7. RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════════════

async def test_reconciliation_empty(client):
    resp = await client.get("/api/v1/finance/reconciliation")
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 8. AUDIT EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

async def test_audit_export(client):
    resp = await client.get("/api/v1/finance/audit-export")
    assert resp.status_code == 200
    data = resp.json()
    assert "records" in data
    assert isinstance(data["records"], list)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. SPEND TREND
# ═══════════════════════════════════════════════════════════════════════════════

async def test_spend_trend_default(client):
    resp = await client.get("/api/v1/finance/spend-trend")
    assert resp.status_code == 200
    data = resp.json()
    assert "days" in data
    assert "series" in data


async def test_spend_trend_with_days(client):
    resp = await client.get("/api/v1/finance/spend-trend?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["days"]) >= 7


# ═══════════════════════════════════════════════════════════════════════════════
# 10. BILLING CONNECTIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_list_billing_connections_empty(client):
    resp = await client.get("/api/v1/finance/connections")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_create_billing_connection(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "openai",
        "provider_name": "OpenAI Production",
        "auth_type": "api_key",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider_type"] == "openai"
    assert data["provider_name"] == "OpenAI Production"
    assert data["status"] == "pending"


# ═══════════════════════════════════════════════════════════════════════════════
# 11. SUMMARY WITH TEAMS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_summary_with_multiple_teams(client):
    await _create_team(client, "sum-t1", "Summary T1", "3000.00")
    await _create_team(client, "sum-t2", "Summary T2", "7000.00")
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    # Verify response shape is correct
    assert "total_monthly_budget_usd" in data
    assert "teams_at_risk" in data
    assert "teams_over_budget" in data
    assert isinstance(data["top_spenders"], list)
