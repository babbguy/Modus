"""Tests for orchestrator.api.finance — all endpoint handlers."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    App,
    AuditLog,
    BillingActual,
    CostCenter,
    Team,
    TeamCostCenter,
    UsageAggregate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _team(slug: str = "fin-team", name: str = "Finance Team", **kw) -> Team:
    return Team(id=str(uuid.uuid4()), slug=slug, name=name, **kw)


def _app(team_id: str, slug: str = "fin-app", **kw) -> App:
    return App(
        id=str(uuid.uuid4()),
        team_id=team_id,
        app_id=slug,
        app_name=f"App {slug}",
        environment="production",
        api_key_hash="x",
        api_key_prefix="mds_test",
        **kw,
    )


def _cost_center(code: str = "CC-100", **kw) -> CostCenter:
    return CostCenter(
        id=str(uuid.uuid4()),
        name=f"CC {code}",
        code=code,
        department="Engineering",
        **kw,
    )


def _usage(team_id: str, app_id: str, cost: str = "10.0", **kw) -> UsageAggregate:
    now = datetime.now(timezone.utc)
    return UsageAggregate(
        id=str(uuid.uuid4()),
        team_id=team_id,
        app_id=app_id,
        provider="openai",
        model="gpt-4o",
        resource_type="chat",
        granularity="daily",
        period_start=now - timedelta(hours=6),
        period_end=now,
        call_count=100,
        input_tokens=5000,
        output_tokens=2000,
        total_tokens=7000,
        total_cost=Decimal(cost),
        **kw,
    )


@pytest_asyncio.fixture
async def seed_finance(db_session: AsyncSession):
    """Seed a team, app, cost center, team-cost-center mapping, and usage."""
    team = _team()
    db_session.add(team)
    await db_session.flush()

    app = _app(team.id)
    db_session.add(app)

    cc = _cost_center()
    db_session.add(cc)
    await db_session.flush()

    tcc = TeamCostCenter(
        id=str(uuid.uuid4()),
        team_id=str(team.id),
        cost_center_id=str(cc.id),
    )
    db_session.add(tcc)

    usage = _usage(str(team.id), str(app.id), "42.50")
    db_session.add(usage)
    await db_session.commit()

    return {"team": team, "app": app, "cc": cc, "tcc": tcc, "usage": usage}


# ── GET /finance/summary ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finance_summary(client, seed_finance):
    resp = await client.get("/api/v1/finance/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_monthly_budget_usd" in data
    assert "total_current_spend_usd" in data
    assert "overall_risk" in data
    assert "top_spenders" in data
    assert "spend_by_department" in data
    assert "recent_invoices" in data
    assert data["cost_center_count"] >= 1


# ── GET /finance/burn-rate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_burn_rate_current(client, seed_finance):
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    assert "teams" in data
    assert "total_spend_usd" in data
    assert "overall_risk" in data


@pytest.mark.asyncio
async def test_burn_rate_prior_month(client, seed_finance):
    resp = await client.get("/api/v1/finance/burn-rate?period=prior_month")
    assert resp.status_code == 200
    data = resp.json()
    assert "teams" in data


@pytest.mark.asyncio
async def test_burn_rate_by_cost_center(client, seed_finance):
    cc_id = str(seed_finance["cc"].id)
    resp = await client.get(f"/api/v1/finance/burn-rate?cost_center_id={cc_id}")
    assert resp.status_code == 200


# ── GET /finance/cost-centers ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_cost_centers(client, seed_finance):
    resp = await client.get("/api/v1/finance/cost-centers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["code"] == "CC-100"


# ── POST /finance/cost-centers ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_cost_center(client):
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "New Center",
        "code": "CC-999",
        "department": "Sales",
        "budget_monthly_usd": "5000.00",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["code"] == "CC-999"
    assert data["department"] == "Sales"


@pytest.mark.asyncio
async def test_create_cost_center_duplicate_code(client, seed_finance):
    resp = await client.post("/api/v1/finance/cost-centers", json={
        "name": "Dup",
        "code": "CC-100",
    })
    assert resp.status_code == 409


# ── GET /finance/allocation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_allocation(client, seed_finance):
    resp = await client.get("/api/v1/finance/allocation")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["cost_center_code"] == "CC-100"


# ── POST /finance/allocation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_allocation(client, seed_finance):
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": str(seed_finance["team"].id),
        "cost_center_id": str(seed_finance["cc"].id),
        "allocation_pct": 75.0,
    })
    assert resp.status_code == 201
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_set_allocation_team_not_found(client, seed_finance):
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": str(uuid.uuid4()),
        "cost_center_id": str(seed_finance["cc"].id),
        "allocation_pct": 100,
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_allocation_cc_not_found(client, seed_finance):
    resp = await client.post("/api/v1/finance/allocation", json={
        "team_id": str(seed_finance["team"].id),
        "cost_center_id": str(uuid.uuid4()),
        "allocation_pct": 100,
    })
    assert resp.status_code == 404


# ── GET /finance/chargeback ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chargeback_default_period(client, seed_finance):
    resp = await client.get("/api/v1/finance/chargeback")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_chargeback_specific_period(client, seed_finance):
    now = datetime.now(timezone.utc)
    period = now.strftime("%Y-%m")
    resp = await client.get(f"/api/v1/finance/chargeback?period={period}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_chargeback_bad_period(client):
    resp = await client.get("/api/v1/finance/chargeback?period=bad")
    assert resp.status_code == 400


# ── POST /finance/chargeback/generate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_chargeback_invoice(client, seed_finance):
    resp = await client.post("/api/v1/finance/chargeback/generate", json={
        "format": "json",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "total_cost_usd" in data
    assert "line_items" in data


# ── GET /finance/scenarios ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_scenarios_empty(client):
    resp = await client.get("/api/v1/finance/scenarios")
    assert resp.status_code == 200
    assert resp.json() == []


# ── POST /finance/scenarios ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_team_add(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Team growth",
        "scenario_type": "team_add",
        "parameters": {"num_apps": 3, "avg_cost_per_app_monthly": 200.0},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["scenario_type"] == "team_add"
    assert "projected_monthly_increase_usd" in data["result"]


@pytest.mark.asyncio
async def test_scenario_usage_scale(client, seed_finance):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Scale test",
        "scenario_type": "usage_scale",
        "parameters": {"scale_factor": 3.0},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "projected_monthly_usd" in data["result"]


@pytest.mark.asyncio
async def test_scenario_model_swap(client, seed_finance):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Model swap",
        "scenario_type": "model_swap",
        "parameters": {
            "from_model": "gpt-4o",
            "to_model": "gpt-4o-mini",
            "cost_ratio": 0.25,
        },
    })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_scenario_budget_change(client, seed_finance):
    team_id = str(seed_finance["team"].id)
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Budget change",
        "scenario_type": "budget_change",
        "parameters": {"team_id": team_id, "new_budget_monthly_usd": 1000},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "risk" in data["result"]


@pytest.mark.asyncio
async def test_scenario_budget_change_no_team(client):
    resp = await client.post("/api/v1/finance/scenarios", json={
        "name": "Bad budget",
        "scenario_type": "budget_change",
        "parameters": {"new_budget_monthly_usd": 500},
    })
    assert resp.status_code == 400


# ── GET /finance/reconciliation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconciliation_empty(client):
    resp = await client.get("/api/v1/finance/reconciliation")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_reconciliation_with_data(client, db_session):
    now = datetime.now(timezone.utc)
    ba = BillingActual(
        id=str(uuid.uuid4()),
        provider="openai",
        service="OpenAI API",
        period_start=now - timedelta(days=30),
        period_end=now,
        actual_cost_usd=Decimal("250.00"),
        inferred_cost_usd=Decimal("245.00"),
        delta_usd=Decimal("5.00"),
        delta_pct=2.0,
    )
    db_session.add(ba)
    await db_session.commit()

    resp = await client.get("/api/v1/finance/reconciliation")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["provider"] == "openai"


@pytest.mark.asyncio
async def test_reconciliation_filter_by_provider(client, db_session):
    now = datetime.now(timezone.utc)
    for prov in ("openai", "anthropic"):
        db_session.add(BillingActual(
            id=str(uuid.uuid4()),
            provider=prov,
            period_start=now - timedelta(days=30),
            period_end=now,
            actual_cost_usd=Decimal("100.00"),
        ))
    await db_session.commit()

    resp = await client.get("/api/v1/finance/reconciliation?provider=anthropic")
    assert resp.status_code == 200
    data = resp.json()
    assert all(r["provider"] == "anthropic" for r in data)


# ── GET /finance/audit-export ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_export(client, db_session):
    db_session.add(AuditLog(
        actor_id="test-actor",
        resource_type="team",
        resource_id="t1",
        action="created",
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/finance/audit-export")
    assert resp.status_code == 200
    data = resp.json()
    assert data["record_count"] >= 1
    assert "records" in data


@pytest.mark.asyncio
async def test_audit_export_filters(client, db_session):
    db_session.add(AuditLog(
        actor_id="actor-1",
        resource_type="policy",
        resource_id="p1",
        action="updated",
        team_id="team-a",
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/finance/audit-export?action=updated&team_id=team-a")
    assert resp.status_code == 200


# ── GET /finance/connections ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_connections_empty(client):
    resp = await client.get("/api/v1/finance/connections")
    assert resp.status_code == 200
    assert resp.json() == []


# ── POST /finance/connections ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_connection(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "openai",
        "provider_name": "OpenAI Prod",
        "auth_type": "api_key",
        "service_type": "ai",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider_type"] == "openai"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_connection_invalid_provider(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "not_real",
        "provider_name": "Bad",
        "auth_type": "api_key",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_connection_invalid_auth_type(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "aws",
        "provider_name": "AWS Prod",
        "auth_type": "magic",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_connection_invalid_service_type(client):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "gcp",
        "provider_name": "GCP",
        "auth_type": "service_account",
        "service_type": "bogus",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_connection_with_credentials(client, encryption_key):
    resp = await client.post("/api/v1/finance/connections", json={
        "provider_type": "anthropic",
        "provider_name": "Anthropic Prod",
        "auth_type": "bearer",
        "credentials": {"api_key": "sk-test-1234"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["has_credentials"] is True
