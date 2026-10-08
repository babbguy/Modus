"""
Tests for orchestrator.api.finance — Integration tests for finance endpoints
using the test client pattern.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from orchestrator.api.finance import (
    _classify_risk,
    _project_eom,
    BurnRateEntry,
    CostCenterRequest,
    AllocationRequest,
    ScenarioRequest,
    ChargebackGenerateRequest,
    BillingConnectionRequest,
)
# ── Helper functions ─────────────────────────────────────────────────────────


class TestClassifyRisk:
    def test_on_track(self):
        assert _classify_risk(Decimal("50")) == "on-track"

    def test_on_track_zero(self):
        assert _classify_risk(Decimal("0")) == "on-track"

    def test_at_risk_80(self):
        assert _classify_risk(Decimal("80")) == "at-risk"

    def test_at_risk_99(self):
        assert _classify_risk(Decimal("99.9")) == "at-risk"

    def test_over_budget_100(self):
        assert _classify_risk(Decimal("100")) == "over-budget"

    def test_over_budget_200(self):
        assert _classify_risk(Decimal("200")) == "over-budget"

    def test_boundary_79(self):
        assert _classify_risk(Decimal("79.9")) == "on-track"


class TestProjectEOM:
    def test_zero_days_returns_current(self):
        result = _project_eom(Decimal("100"), 0, 30)
        assert result == Decimal("100")

    def test_negative_days_returns_current(self):
        result = _project_eom(Decimal("100"), -1, 30)
        assert result == Decimal("100")

    def test_mid_month_projection(self):
        result = _project_eom(Decimal("500"), 15, 30)
        expected = (Decimal("500") / Decimal("15") * Decimal("30")).quantize(Decimal("0.01"))
        assert result == expected

    def test_full_month_no_change(self):
        result = _project_eom(Decimal("3000"), 30, 30)
        assert result == Decimal("3000.00")

    def test_early_month_high_projection(self):
        result = _project_eom(Decimal("100"), 1, 30)
        assert result == Decimal("3000.00")


# ── Schema validation ────────────────────────────────────────────────────────


class TestSchemas:
    def test_burn_rate_entry(self):
        entry = BurnRateEntry(
            team_slug="eng",
            team_name="Engineering",
            current_spend_usd="500.00",
            projected_eom_usd="1000.00",
            burn_pct="50.0",
            risk="on-track",
            days_remaining=15,
        )
        assert entry.team_slug == "eng"
        assert entry.cost_center_code is None

    def test_cost_center_request_validation(self):
        req = CostCenterRequest(name="Engineering", code="ENG-001")
        assert req.name == "Engineering"
        assert req.code == "ENG-001"
        assert req.department is None

    def test_cost_center_request_empty_name_fails(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CostCenterRequest(name="", code="ENG-001")

    def test_allocation_request_bounds(self):
        from pydantic import ValidationError
        req = AllocationRequest(team_id="t1", cost_center_id="cc1", allocation_pct=50.0)
        assert req.allocation_pct == 50.0

        with pytest.raises(ValidationError):
            AllocationRequest(team_id="t1", cost_center_id="cc1", allocation_pct=101)

    def test_scenario_request_valid_types(self):
        for st in ("model_swap", "team_add", "usage_scale", "budget_change"):
            req = ScenarioRequest(name="Test", scenario_type=st, parameters={})
            assert req.scenario_type == st

    def test_scenario_request_invalid_type(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ScenarioRequest(name="Test", scenario_type="invalid", parameters={})

    def test_chargeback_generate_format(self):
        req = ChargebackGenerateRequest(format="json")
        assert req.format == "json"
        req2 = ChargebackGenerateRequest(format="csv")
        assert req2.format == "csv"

    def test_chargeback_generate_invalid_format(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ChargebackGenerateRequest(format="xml")

    def test_billing_connection_request(self):
        req = BillingConnectionRequest(
            provider_type="aws",
            provider_name="AWS Main",
            auth_type="api_key",
        )
        assert req.provider_type == "aws"
        assert req.credentials is None


# ── API integration tests ────────────────────────────────────────────────────


async def test_finance_summary_empty(client):
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_monthly_budget_usd"] == "0.00"
    assert data["total_current_spend_usd"] == "0.00"
    assert data["cost_center_count"] == 0


async def test_burn_rate_empty(client):
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["teams"] == []
    assert data["total_budget_usd"] == "0.00"


async def test_burn_rate_prior_month(client):
    resp = await client.get("/api/v1/finance/burn-rate", params={"period": "prior_month"})
    assert resp.status_code == 200
    data = resp.json()
    assert "teams" in data


async def test_list_cost_centers_empty(client):
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_cost_center(client):
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Engineering",
        "code": "ENG-001",
        "department": "Engineering",
        "budget_monthly_usd": "10000.00",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Engineering"
    assert data["code"] == "ENG-001"
    assert data["is_active"] is True
    assert data["team_count"] == 0


async def test_create_duplicate_cost_center(client):
    await client.post("/api/v1/finance/cost-centers", json={
        "name": "Engineering", "code": "DUP-001",
    })
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Engineering 2", "code": "DUP-001",
    })
    assert resp.status_code == 409


async def test_get_allocation_empty(client):
    resp = await client.get("/api/v1/finance/allocation")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_chargeback_empty(client):
    resp = await client.get("/api/v1/finance/chargeback")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_chargeback_with_period(client):
    resp = await client.get("/api/v1/finance/chargeback", params={"period": "2026-04"})
    assert resp.status_code == 200


async def test_get_chargeback_invalid_period(client):
    resp = await client.get("/api/v1/finance/chargeback", params={"period": "invalid"})
    assert resp.status_code == 400


async def test_audit_export_empty(client):
    resp = await client.get("/api/v1/finance/audit-export")
    assert resp.status_code == 200
