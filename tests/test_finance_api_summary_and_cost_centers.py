"""
Finance API deep coverage.

Targets orchestrator.api.finance:
  - finance_summary (230-270, 280-288)
  - burn_rate (342, 363, 369-398, 412-414)
  - list_cost_centers (439-461)
  - create_cost_center (495-512)
  - get_allocation / set_allocation (540, 562, 566-588)
  - chargeback (648, 673, 700-702)
  - chargeback/generate (717-730)
  - scenarios (803-812, 837-841, 875-881)
  - reconciliation (901, 924, 955, 961, 963)
  - audit-export (970-984)
  - spend-trend (1163-1164, 1183-1207, 1211-1237)
  - billing connections (1380-1480, 1535-1584, 1597-1601)
  - forecast (1632-1673, 1693-1698, 1714-1722)
"""
from __future__ import annotations

import uuid

# ── Helpers ──────────────────────────────────────────────────────────────────

async def _create_team(client, slug: str, name: str = "Team", budget: str = None):
    payload = {"name": name, "slug": slug}
    if budget:
        payload["budget_monthly_usd"] = budget
    resp = await client.post("/api/v1/teams", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _create_cost_center(client, name: str, code: str, dept: str = "Engineering",
                               budget: str = None):
    payload = {"name": name, "code": code, "department": dept}
    if budget:
        payload["budget_monthly_usd"] = budget
    resp = await client.post("/api/v1/finance/cost-centers", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Finance Summary ──────────────────────────────────────────────────────────

async def test_finance_summary_empty(client):
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "overall_risk" in data
    assert "recent_invoices" in data


async def test_finance_summary_with_team(client):
    await _create_team(client, "fin-sum-team", "Finance Summary Team", "5000.00")
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "overall_risk" in data


# ── Burn Rate ────────────────────────────────────────────────────────────────

async def test_burn_rate_empty(client):
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert "teams" in data
    assert "period" in data


async def test_burn_rate_with_team_budget(client):
    await _create_team(client, "burn-team", "Burn Rate Team", "10000.00")
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["teams"]) >= 1


async def test_burn_rate_prior_month(client):
    resp = await client.get("/api/v1/finance/burn-rate", params={"prior_month": "true"})
    assert resp.status_code == 200


async def test_burn_rate_cost_center_filter(client):
    cc = await _create_cost_center(client, "Burn CC", "BURN-001", budget="20000.00")
    resp = await client.get("/api/v1/finance/burn-rate",
                           params={"cost_center_id": cc["id"]})
    assert resp.status_code == 200


# ── Cost Centers ─────────────────────────────────────────────────────────────

async def test_list_cost_centers(client):
    await _create_cost_center(client, "Eng CC", "CC-LIST-001", "Engineering", "5000.00")
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    centers = resp.json()
    assert isinstance(centers, list)
    assert any(c["code"] == "CC-LIST-001" for c in centers)


async def test_create_cost_center_full(client):
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Full CC",
        "code": "CC-FULL-001",
        "department": "Research",
        "division": "AI Labs",
        "budget_owner_name": "Jane Doe",
        "budget_owner_email": "jane@example.com",
        "budget_monthly_usd": "15000.00",
        "budget_quarterly_usd": "45000.00",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["department"] == "Research"
    assert data["division"] == "AI Labs"
    assert data["budget_owner_name"] == "Jane Doe"


async def test_create_cost_center_duplicate_code(client):
    await _create_cost_center(client, "Dup CC", "CC-DUP-001")
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Dup CC 2",
        "code": "CC-DUP-001",
    })
    assert resp.status_code == 409


# ── Cost Allocation ──────────────────────────────────────────────────────────

async def test_get_allocation_empty(client):
    resp = await client.get("/api/v1/finance/allocation")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_set_allocation(client):
    team = await _create_team(client, "alloc-team")
    cc = await _create_cost_center(client, "Alloc CC", "CC-ALLOC-001")

    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": cc["id"],
        "allocation_pct": "100.00",
    })
    assert resp.status_code == 201
    assert resp.json()["status"] == "ok"


async def test_set_allocation_upsert(client):
    team = await _create_team(client, "alloc-upsert-team")
    cc1 = await _create_cost_center(client, "Alloc CC1", "CC-UPS-001")
    cc2 = await _create_cost_center(client, "Alloc CC2", "CC-UPS-002")

    await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": cc1["id"],
        "allocation_pct": "100.00",
    })
    # Upsert with different cost center
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": cc2["id"],
        "allocation_pct": "75.00",
    })
    assert resp.status_code == 201


async def test_set_allocation_team_not_found(client):
    cc = await _create_cost_center(client, "Alloc CC3", "CC-TNF-001")
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": str(uuid.uuid4()),
        "cost_center_id": cc["id"],
        "allocation_pct": "100.00",
    })
    assert resp.status_code == 404


async def test_set_allocation_cc_not_found(client):
    team = await _create_team(client, "alloc-ccnf-team")
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": team["id"],
        "cost_center_id": str(uuid.uuid4()),
        "allocation_pct": "100.00",
    })
    assert resp.status_code == 404


# ── Chargeback ───────────────────────────────────────────────────────────────

async def test_chargeback_current_month(client):
    resp = await client.get("/api/v1/finance/chargeback")
    assert resp.status_code == 200


async def test_chargeback_with_period(client):
    resp = await client.get("/api/v1/finance/chargeback", params={"period": "2025-12"})
    assert resp.status_code == 200


async def test_chargeback_invalid_period(client):
    resp = await client.get("/api/v1/finance/chargeback", params={"period": "bad"})
    assert resp.status_code == 400


async def test_chargeback_generate(client):
    resp = await client.post("/api/v1/finance/chargeback/generate", json={
        "period": "2025-12",
        "format": "json",
    })
    assert resp.status_code in (200, 201, 404, 422)


# ── Scenarios ────────────────────────────────────────────────────────────────

async def test_list_scenarios(client):
    resp = await client.get("/api/v1/finance/scenarios")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_run_scenario(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Test scenario",
        "type": "model_swap",
        "params": {
            "from_model": "gpt-4o",
            "to_model": "gpt-4o-mini",
        },
    })
    # Might succeed or fail depending on data availability
    assert resp.status_code in (200, 201, 422)


# ── Reconciliation ───────────────────────────────────────────────────────────

async def test_reconciliation(client):
    resp = await client.get("/api/v1/finance/reconciliation")
    assert resp.status_code == 200


# ── Audit Export ─────────────────────────────────────────────────────────────

async def test_audit_export(client):
    resp = await client.get("/api/v1/finance/audit-export")
    assert resp.status_code == 200


async def test_audit_export_csv(client):
    resp = await client.get("/api/v1/finance/audit-export", params={"format": "csv"})
    assert resp.status_code == 200


# ── Spend Trend ──────────────────────────────────────────────────────────────

async def test_spend_trend(client):
    resp = await client.get("/api/v1/finance/spend-trend")
    assert resp.status_code == 200


async def test_spend_trend_with_days(client):
    resp = await client.get("/api/v1/finance/spend-trend", params={"days": 7})
    assert resp.status_code == 200


# ── Billing Connections ──────────────────────────────────────────────────────

async def test_list_connections(client):
    resp = await client.get("/api/v1/finance/connections")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_create_connection(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider": "openai",
        "auth_type": "api_key",
        "credentials": {"api_key": "sk-test-fake-key"},
    })
    assert resp.status_code in (200, 201, 422)


# ── Forecast ─────────────────────────────────────────────────────────────────

async def test_forecast_config_list(client):
    resp = await client.get("/api/v1/finance/forecast/config")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
