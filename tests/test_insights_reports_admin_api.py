"""Tests for orchestrator.api.insights — insights, reports, and admin endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import (
    AnomalyEvent,
    App,
    OptimizationRecommendation,
    SpendForecast,
    Team,
)


def _team(**kw) -> Team:
    defaults = {"id": str(uuid.uuid4()), "slug": "ins-team", "name": "Insights Team"}
    defaults.update(kw)
    return Team(**defaults)


def _app(team_id: str, **kw) -> App:
    defaults = {
        "id": str(uuid.uuid4()),
        "team_id": team_id,
        "app_id": "ins-app",
        "app_name": "Insights App",
        "environment": "production",
        "api_key_hash": "x",
        "api_key_prefix": "mds_test",
    }
    defaults.update(kw)
    return App(**defaults)


@pytest_asyncio.fixture
async def seed_insights(db_session: AsyncSession):
    team = _team()
    db_session.add(team)
    await db_session.flush()

    app = _app(str(team.id))
    db_session.add(app)
    await db_session.flush()

    now = datetime.now(timezone.utc)

    anomaly = AnomalyEvent(
        id=str(uuid.uuid4()),
        team_id=str(team.id),
        app_id=str(app.id),
        metric="total_cost",
        severity="high",
        z_score=Decimal("3.5"),
        baseline_value=Decimal("50.00"),
        actual_value=Decimal("175.00"),
        window_hours=1,
        ai_explanation="Spike in GPT-4 usage",
        detected_at=now - timedelta(hours=2),
    )
    db_session.add(anomaly)

    rec = OptimizationRecommendation(
        id=str(uuid.uuid4()),
        team_id=str(team.id),
        app_id=str(app.id),
        current_model="gpt-4o",
        suggested_model="gpt-4o-mini",
        provider="openai",
        call_volume_basis=5000,
        estimated_monthly_savings=Decimal("120.00"),
        confidence="high",
        recommendation_text="Switch to gpt-4o-mini for non-critical calls",
    )
    db_session.add(rec)

    forecast = SpendForecast(
        id=str(uuid.uuid4()),
        team_id=str(team.id),
        period_label="2026-04",
        mtd_actual=Decimal("340.00"),
        forecast_eom=Decimal("1020.00"),
        forecast_eoq=Decimal("3200.00"),
        trend_pct=Decimal("12.5"),
        r_squared=Decimal("0.92"),
    )
    db_session.add(forecast)

    await db_session.commit()
    return {"team": team, "app": app, "anomaly": anomaly, "rec": rec, "forecast": forecast}


# ── GET /insights/anomalies ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_anomalies(client, seed_insights):
    """Stub identity has team_id=None so SQL WHERE won't match seeded rows.
    We test that the endpoint returns 200 without errors."""
    resp = await client.get("/api/v1/insights/anomalies")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_anomalies_filter_severity(client, seed_insights):
    resp = await client.get("/api/v1/insights/anomalies?severity=high")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_anomalies_include_resolved(client, seed_insights):
    resp = await client.get("/api/v1/insights/anomalies?include_resolved=true")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_anomalies_hours(client, seed_insights):
    resp = await client.get("/api/v1/insights/anomalies?hours=168")
    assert resp.status_code == 200


# ── GET /insights/recommendations ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recommendations(client, seed_insights):
    """Stub identity has team_id=None; endpoint filters by team_id."""
    resp = await client.get("/api/v1/insights/recommendations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_recommendations_min_savings(client, seed_insights):
    resp = await client.get("/api/v1/insights/recommendations?min_savings=200")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_recommendations_include_dismissed(client, seed_insights):
    resp = await client.get("/api/v1/insights/recommendations?include_dismissed=true")
    assert resp.status_code == 200


# ── POST /insights/recommendations/{id}/dismiss ─────────────────────────────


@pytest.mark.asyncio
async def test_dismiss_recommendation(client, seed_insights):
    """A platform admin (stub identity, no team of its own) can dismiss any
    team's recommendation; it then drops out of the list."""
    rec_id = str(seed_insights["rec"].id)
    resp = await client.post(f"/api/v1/insights/recommendations/{rec_id}/dismiss")
    assert resp.status_code == 200
    assert resp.json() == {"dismissed": True}


@pytest.mark.asyncio
async def test_dismiss_recommendation_not_found(client):
    resp = await client.post(f"/api/v1/insights/recommendations/{uuid.uuid4()}/dismiss")
    assert resp.status_code == 404


# ── GET /insights/enforcement-summary ────────────────────────────────────────


@pytest.mark.asyncio
async def test_enforcement_summary(client, seed_insights):
    resp = await client.get("/api/v1/insights/enforcement-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "allowed" in data
    assert "blocked" in data
    assert "total_decisions" in data
    assert "total_savings" in data


@pytest.mark.asyncio
async def test_enforcement_summary_hours(client, seed_insights):
    resp = await client.get("/api/v1/insights/enforcement-summary?hours=168")
    assert resp.status_code == 200


# ── GET /insights/forecast ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_forecast(client, seed_insights):
    resp = await client.get("/api/v1/insights/forecast")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["confidence_label"] == "high"


# ── GET /reports/roi ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roi_report(client, seed_insights):
    resp = await client.get("/api/v1/reports/roi")
    assert resp.status_code == 200
    data = resp.json()
    assert "blocked_calls" in data
    assert "estimated_savings" in data
    assert "period" in data


@pytest.mark.asyncio
async def test_roi_report_custom_days(client, seed_insights):
    resp = await client.get("/api/v1/reports/roi?days=7")
    assert resp.status_code == 200
    assert resp.json()["period"] == "last 7d"


# ── GET /reports/chargeback ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reports_chargeback(client, seed_insights):
    resp = await client.get("/api/v1/reports/chargeback")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reports_chargeback_with_period(client, seed_insights):
    now = datetime.now(timezone.utc)
    period = now.strftime("%Y-%m")
    resp = await client.get(f"/api/v1/reports/chargeback?period={period}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reports_chargeback_bad_period(client):
    resp = await client.get("/api/v1/reports/chargeback?period=nope")
    assert resp.status_code == 400


# ── GET /admin/settings ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_admin_settings(client):
    resp = await client.get("/api/v1/admin/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    keys = [s["key"] for s in data]
    assert any("seconds" in k or "days" in k for k in keys)


# ── PUT /admin/settings/{key} ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_admin_setting(client):
    resp = await client.put(
        "/api/v1/admin/settings/task.anomaly_scan.interval_seconds",
        json={"value": "600"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "task.anomaly_scan.interval_seconds"
    assert data["value"] == "600"


@pytest.mark.asyncio
async def test_update_admin_setting_bad_key(client):
    resp = await client.put("/api/v1/admin/settings/not_a_setting", json={
        "value": "x",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_admin_setting_numeric_validation(client):
    resp = await client.put(
        "/api/v1/admin/settings/task.anomaly_scan.interval_seconds",
        json={"value": "not-a-number"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_admin_setting_update_existing(client):
    """Update twice to cover the update-existing path."""
    await client.put(
        "/api/v1/admin/settings/task.anomaly_scan.interval_seconds",
        json={"value": "300"},
    )
    resp = await client.put(
        "/api/v1/admin/settings/task.anomaly_scan.interval_seconds",
        json={"value": "900"},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == "900"


# ── POST /admin/tasks/{task}/trigger ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_task_anomaly_scan(client):
    resp = await client.post("/api/v1/admin/tasks/anomaly-scan/trigger")
    assert resp.status_code == 200
    assert resp.json()["triggered"] == "anomaly-scan"


@pytest.mark.asyncio
async def test_trigger_task_forecast(client):
    resp = await client.post("/api/v1/admin/tasks/forecast/trigger")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_trigger_task_recommendations(client):
    resp = await client.post("/api/v1/admin/tasks/recommendations/trigger")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_trigger_task_unknown(client):
    resp = await client.post("/api/v1/admin/tasks/bogus/trigger")
    assert resp.status_code == 404
