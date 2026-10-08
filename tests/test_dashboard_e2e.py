"""
End-to-end dashboard read-path tests.

These tests seed a small synthetic dataset (team + app + usage + alert)
directly into the database, then verify every dashboard endpoint
correctly reflects the seeded data. This catches the class of bug where
a query joins/groups/filters incorrectly — something contract tests
(which run against an empty DB) cannot detect.

Demo walkthrough confidence: if these pass, every dashboard tile renders
real numbers when the underlying data exists.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio

from orchestrator.db.models import (
    Team, App, UsageAggregate, Alert, Threshold,
)
# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def seeded(db_session):
    """Seed a deterministic test dataset.

    Returns a dict with the entity ids the tests can reference.
    """
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 1 team
    team = Team(
        id=str(uuid.uuid4()),
        slug="acme-eng",
        name="Acme Engineering",
        budget_monthly_usd=Decimal("5000.00"),
    )
    db_session.add(team)
    await db_session.flush()

    # 2 apps in the team
    app1 = App(
        id=str(uuid.uuid4()),
        team_id=team.id,
        app_id="acme-rag-prod",
        app_name="Acme RAG (production)",
        environment="production",
        is_active=True,
        api_key_hash="dummy_hash_1",
        api_key_prefix="mds_test_1",
        last_seen_at=now - timedelta(minutes=2),
    )
    app2 = App(
        id=str(uuid.uuid4()),
        team_id=team.id,
        app_id="acme-rag-staging",
        app_name="Acme RAG (staging)",
        environment="staging",
        is_active=True,
        api_key_hash="dummy_hash_2",
        api_key_prefix="mds_test_2",
        last_seen_at=now - timedelta(hours=1),
    )
    db_session.add_all([app1, app2])
    await db_session.flush()

    # 7 days of daily usage rollups for each app
    for day_offset in range(7):
        period = today - timedelta(days=day_offset)
        for app, base_cost in ((app1, Decimal("12.50")), (app2, Decimal("3.25"))):
            db_session.add(UsageAggregate(
                id=str(uuid.uuid4()),
                team_id=team.id,
                app_id=app.id,
                provider="anthropic",
                model="claude-3-5-sonnet-20241022",
                resource_type="llm",
                granularity="daily",
                period_start=period,
                period_end=period + timedelta(days=1),
                input_tokens=10_000 + day_offset * 100,
                output_tokens=2_000 + day_offset * 50,
                total_tokens=12_000 + day_offset * 150,
                call_count=42 + day_offset,
                total_cost=base_cost + Decimal(day_offset),
            ))
            db_session.add(UsageAggregate(
                id=str(uuid.uuid4()),
                team_id=team.id,
                app_id=app.id,
                provider="openai",
                model="gpt-4o-mini",
                resource_type="llm",
                granularity="daily",
                period_start=period,
                period_end=period + timedelta(days=1),
                input_tokens=5_000 + day_offset * 50,
                output_tokens=1_500 + day_offset * 25,
                total_tokens=6_500 + day_offset * 75,
                call_count=20 + day_offset,
                total_cost=(base_cost / Decimal("4")) + Decimal(day_offset) / Decimal("2"),
            ))

    # Threshold (alerts have a NOT NULL threshold_id FK)
    threshold = Threshold(
        id=str(uuid.uuid4()),
        team_id=team.id,
        app_id=app1.id,
        name="daily cost cap",
        scope="app",
        metric="total_cost",
        period="daily",
        critical_value=Decimal("10.00"),
        warning_value=Decimal("8.00"),
        is_active=True,
    )
    db_session.add(threshold)
    await db_session.flush()

    # 2 alerts
    db_session.add(Alert(
        id=str(uuid.uuid4()),
        threshold_id=threshold.id,
        team_id=team.id,
        app_id=app1.id,
        severity="warning",
        metric="daily_cost",
        threshold_value=Decimal("10.00"),
        actual_value=Decimal("15.75"),
        period_start=today,
        period_end=today + timedelta(days=1),
        fired_at=now - timedelta(hours=3),
        notification_sent=True,
        notification_result={"slack": True},
    ))
    db_session.add(Alert(
        id=str(uuid.uuid4()),
        threshold_id=threshold.id,
        team_id=team.id,
        app_id=app2.id,
        severity="info",
        metric="latency_p95",
        threshold_value=Decimal("1500"),
        actual_value=Decimal("1820"),
        period_start=today,
        period_end=today + timedelta(days=1),
        fired_at=now - timedelta(hours=8),
        notification_sent=False,
        notification_result={"slack": False},
    ))

    await db_session.commit()

    return {
        "team_id": team.id,
        "team_slug": team.slug,
        "app1_id": app1.id,
        "app1_app_id": app1.app_id,
        "app2_id": app2.id,
        "app2_app_id": app2.app_id,
    }


# ── End-to-end read tests ──────────────────────────────────────────────────


async def test_e2e_summary_reflects_seeded_data(client, seeded):
    """After seeding 7 days of usage, the dashboard summary endpoint
    must return non-zero cost numbers."""
    resp = await client.get("/api/v1/dashboard/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert float(data.get("total_cost_today") or 0) > 0, (
        f"summary returned zero cost despite seeded usage: {data}"
    )
    assert data.get("active_apps", 0) >= 2


async def test_e2e_by_app_returns_seeded_apps(client, seeded):
    """by-app should return both seeded apps with non-zero cost."""
    resp = await client.get("/api/v1/dashboard/by-app?days=7&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    app_ids = {a["app_id"] for a in data}
    assert seeded["app1_app_id"] in app_ids, f"app1 missing from by-app: got {app_ids}"
    assert seeded["app2_app_id"] in app_ids, f"app2 missing from by-app: got {app_ids}"

    for a in data:
        assert float(a["cost"]) > 0, f"by-app returned zero cost for {a['app_id']}"
        assert a["calls"] > 0
        assert a["tokens"] > 0


async def test_e2e_by_provider_aggregates_correctly(client, seeded):
    """by-provider must show both providers (anthropic + openai) and
    their cost percentages must sum to ~100%."""
    resp = await client.get("/api/v1/dashboard/by-provider?days=7")
    assert resp.status_code == 200
    data = resp.json()
    providers = {p["provider"] for p in data}
    assert "anthropic" in providers
    assert "openai" in providers

    pct_total = sum(float(p["cost_pct"]) for p in data)
    assert 99.0 <= pct_total <= 101.0, f"cost_pct sum is {pct_total}, expected ~100"


async def test_e2e_top_models_returns_models(client, seeded):
    """top-models must return both seeded models."""
    resp = await client.get("/api/v1/dashboard/top-models?days=7&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    models = {m["model"] for m in data}
    assert "claude-3-5-sonnet-20241022" in models
    assert "gpt-4o-mini" in models


async def test_e2e_top_models_app_id_filter_isolates_one_app(client, seeded):
    """GAP-2 fix: top-models with app_id filter must return only that
    app's models. Without the filter we'd see all models."""
    full = (await client.get("/api/v1/dashboard/top-models?days=7&limit=10")).json()
    filtered = (await client.get(
        f"/api/v1/dashboard/top-models?days=7&limit=10&app_id={seeded['app1_app_id']}"
    )).json()
    # Both apps use the same models, so the model set should be equal
    # but the cost in `filtered` must be lower (only one app's spend).
    full_cost = sum(float(m["cost"]) for m in full)
    filt_cost = sum(float(m["cost"]) for m in filtered)
    assert filt_cost < full_cost, (
        f"GAP-2 fix regression: app_id filter is being ignored. "
        f"full={full_cost} filtered={filt_cost}"
    )


async def test_e2e_recent_alerts_returns_seeded_alerts(client, seeded):
    """recent-alerts must include both seeded alerts with notification fields."""
    resp = await client.get("/api/v1/dashboard/recent-alerts?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2

    for alert in data:
        assert "notification_sent" in alert
        assert "notification_result" in alert

    # At least one alert should have notification_sent=True (we seeded one)
    assert any(a.get("notification_sent") is True for a in data), (
        "GAP-8 regression: notification_sent is never true despite seeded data"
    )


async def test_e2e_recent_alerts_app_id_filter(client, seeded):
    """GAP-2 fix: recent-alerts with app_id filter must return only that
    app's alerts."""
    full = (await client.get("/api/v1/dashboard/recent-alerts?limit=10")).json()
    filtered = (await client.get(
        f"/api/v1/dashboard/recent-alerts?limit=10&app_id={seeded['app1_id']}"
    )).json()
    assert len(filtered) <= len(full)
    for a in filtered:
        assert a["app_id"] == seeded["app1_id"], (
            f"GAP-2 fix regression: app_id filter not honored on alerts. "
            f"got app_id={a['app_id']} expected {seeded['app1_id']}"
        )


async def test_e2e_app_status_returns_apps(client, seeded):
    """app-status must show both apps with online flags."""
    resp = await client.get("/api/v1/dashboard/app-status")
    assert resp.status_code == 200
    data = resp.json()
    app_ids = {a["app_id"] for a in data}
    assert seeded["app1_app_id"] in app_ids
    assert seeded["app2_app_id"] in app_ids

    # app1 was last_seen 2 minutes ago — should be online
    # app2 was last_seen 1 hour ago — should be offline (>5 min stale)
    by_id = {a["app_id"]: a for a in data}
    assert by_id[seeded["app1_app_id"]]["online"] is True, "app1 should be online"
    assert by_id[seeded["app2_app_id"]]["online"] is False, "app2 should be offline"


async def test_e2e_finance_burn_rate_includes_team(client, seeded):
    """Finance burn-rate must show the seeded team."""
    resp = await client.get("/api/v1/finance/burn-rate")
    assert resp.status_code == 200
    data = resp.json()
    team_names = {t["team_name"] for t in data["teams"]}
    assert "Acme Engineering" in team_names


async def test_e2e_cost_over_time_returns_7_days(client, seeded):
    """cost-over-time with days=7 must return at least 7 daily points
    with non-zero cost."""
    resp = await client.get("/api/v1/dashboard/cost-over-time?days=7&granularity=daily")
    assert resp.status_code == 200
    data = resp.json()
    nonzero = [p for p in data if float(p.get("cost") or 0) > 0]
    assert len(nonzero) >= 7, (
        f"expected ≥7 days of nonzero cost data, got {len(nonzero)} of {len(data)}"
    )
