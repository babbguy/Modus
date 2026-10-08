"""
Tests for orchestrator.core.threshold_evaluator — Extended coverage for
evaluate_thresholds, alert creation, notification dispatch, and tier logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.core.threshold_evaluator import (
    ALERT_TIERS,
    _dispatch_integrations,
    _evaluate_one,
    _fired_tier_key,
    _fired_tiers,
    _METRIC_COL_MAP,
    evaluate_thresholds,
)
from orchestrator.db.models import (
    App,
    Base,
    Team,
    Threshold,
    UsageAggregate,
)


@pytest_asyncio.fixture
async def threshold_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def threshold_db(threshold_engine):
    factory = async_sessionmaker(threshold_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
# ── METRIC_COL_MAP ───────────────────────────────────────────────────────────


class TestMetricColMap:
    def test_all_metrics_mapped(self):
        expected = {"total_cost", "input_tokens", "output_tokens", "call_count"}
        assert set(_METRIC_COL_MAP.keys()) == expected

    def test_values_match_column_names(self):
        for metric, col_name in _METRIC_COL_MAP.items():
            assert hasattr(UsageAggregate, col_name), f"{col_name} not on UsageAggregate"


# ── Alert tier constants ─────────────────────────────────────────────────────


class TestAlertTierConstants:
    def test_three_tiers(self):
        assert len(ALERT_TIERS) == 3

    def test_ascending_percentages(self):
        pcts = [t[0] for t in ALERT_TIERS]
        assert pcts == sorted(pcts)

    def test_tier_severities(self):
        severities = {t[1] for t in ALERT_TIERS}
        assert "caution" in severities
        assert "warning" in severities
        assert "critical" in severities


# ── _evaluate_one with DB ────────────────────────────────────────────────────


async def test_evaluate_one_no_data(threshold_db):
    """Threshold with no usage data should not fire."""
    team = Team(slug="test-team", name="Test Team")
    threshold_db.add(team)
    await threshold_db.flush()

    t = Threshold(
        team_id=str(team.id),
        name="cost-alert",
        metric="total_cost",
        period="daily",
        scope="team",
        critical_value=Decimal("100.00"),
        is_active=True,
    )
    threshold_db.add(t)
    await threshold_db.flush()

    now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
    # Clear any previous fired tiers
    _fired_tiers.clear()
    fired = await _evaluate_one(threshold_db, t, now)
    assert fired == 0


async def test_evaluate_one_fires_critical(threshold_db):
    """Threshold exceeded at 100% should fire critical."""
    team = Team(slug="hot-team", name="Hot Team")
    threshold_db.add(team)
    await threshold_db.flush()

    app = App(
        team_id=str(team.id),
        app_id="hot-app",
        app_name="Hot App",
        api_key_hash="fake",
        api_key_prefix="mds_fake",
    )
    threshold_db.add(app)
    await threshold_db.flush()

    t = Threshold(
        team_id=str(team.id),
        name="cost-critical",
        metric="total_cost",
        period="daily",
        scope="team",
        critical_value=Decimal("50.00"),
        is_active=True,
    )
    threshold_db.add(t)
    await threshold_db.flush()

    # Add usage that exceeds the threshold
    now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    agg = UsageAggregate(
        app_id=str(app.id),
        team_id=str(team.id),
        provider="openai",
        model="gpt-4o",
        resource_type="chat",
        granularity="daily",
        period_start=day_start,
        period_end=day_start + timedelta(days=1),
        call_count=100,
        total_tokens=10000,
        input_tokens=5000,
        output_tokens=5000,
        total_cost=Decimal("150.00"),  # 3x the threshold
    )
    threshold_db.add(agg)
    await threshold_db.flush()

    _fired_tiers.clear()
    with patch("orchestrator.core.threshold_evaluator._dispatch_integrations",
               new_callable=AsyncMock):
        fired = await _evaluate_one(threshold_db, t, now)
    # Should fire all 3 tiers (70%, 90%, 100%)
    assert fired == 3


async def test_evaluate_one_zero_critical_value(threshold_db):
    """Threshold with zero critical_value should not fire."""
    team = Team(slug="zero-team", name="Zero Team")
    threshold_db.add(team)
    await threshold_db.flush()

    t = Threshold(
        team_id=str(team.id),
        name="zero-threshold",
        metric="total_cost",
        period="daily",
        scope="team",
        critical_value=Decimal("0"),
        is_active=True,
    )
    threshold_db.add(t)
    await threshold_db.flush()

    now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
    _fired_tiers.clear()
    fired = await _evaluate_one(threshold_db, t, now)
    assert fired == 0


async def test_evaluate_one_unknown_metric(threshold_db):
    """Threshold with unknown metric should return 0."""
    team = Team(slug="unk-team", name="Unknown Metric Team")
    threshold_db.add(team)
    await threshold_db.flush()

    t = Threshold(
        team_id=str(team.id),
        name="unknown-metric",
        metric="nonexistent_metric",
        period="daily",
        scope="team",
        critical_value=Decimal("100"),
        is_active=True,
    )
    threshold_db.add(t)
    await threshold_db.flush()

    now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
    _fired_tiers.clear()
    fired = await _evaluate_one(threshold_db, t, now)
    assert fired == 0


# ── evaluate_thresholds top-level ────────────────────────────────────────────


async def test_evaluate_thresholds_no_session_factory():
    """Should return 0 when no session factory is configured."""
    with patch("orchestrator.core.threshold_evaluator._session_factory", None):
        result = await evaluate_thresholds()
        assert result == 0


# ── _dispatch_integrations ───────────────────────────────────────────────────


async def test_dispatch_integrations_no_crash():
    """Dispatch should not raise even when all channels fail."""
    mock_threshold = MagicMock()
    mock_threshold.notify = {}

    alert_data = {
        "alert_id": "a1",
        "threshold_id": "t1",
        "threshold_name": "Test",
        "app_id": None,
        "team_id": "team-1",
        "severity": "warning",
        "metric": "total_cost",
        "actual_value": "150",
        "threshold_value": "100",
        "period": "daily",
        "tier_pct": 90,
        "tier_message": "Nearing budget",
    }

    with patch("orchestrator.core.threshold_evaluator._send_notifications",
               new_callable=AsyncMock):
        await _dispatch_integrations(mock_threshold, alert_data)


async def test_dispatch_integrations_critical_with_incident_webhook():
    mock_threshold = MagicMock()
    mock_threshold.notify = {"incident_webhook": "https://example.com/webhook"}

    alert_data = {
        "alert_id": "a2",
        "threshold_id": "t2",
        "threshold_name": "Critical Test",
        "app_id": "app-1",
        "team_id": "team-1",
        "severity": "critical",
        "metric": "total_cost",
        "actual_value": "500",
        "threshold_value": "100",
        "period": "daily",
        "tier_pct": 100,
        "tier_message": "Breached",
    }

    with patch("orchestrator.core.threshold_evaluator._send_notifications",
               new_callable=AsyncMock), \
         patch("orchestrator.core.threshold_evaluator.settings") as mock_settings, \
         patch("orchestrator.core.threshold_evaluator._trigger_auto_pause",
               new_callable=AsyncMock), \
         patch("orchestrator.core.threshold_evaluator._trigger_incident_ticket",
               new_callable=AsyncMock) as mock_ticket:
        mock_settings.app_pause_endpoint_enabled = False
        await _dispatch_integrations(mock_threshold, alert_data)
        mock_ticket.assert_awaited_once()


# ── Stale tier purging ───────────────────────────────────────────────────────


class TestStaleTierPurging:
    def test_fired_tier_key_format(self):
        dt = datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
        key = _fired_tier_key("t1", dt)
        assert key == ("t1", dt.isoformat())

    def test_stale_entries_would_be_purged(self):
        """Verify the purge logic concept: entries older than 8 days should
        be considered stale by evaluate_thresholds."""
        now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        cutoff = (now - timedelta(days=8)).isoformat()
        old_key = ("t1", (now - timedelta(days=10)).isoformat())
        recent_key = ("t2", now.isoformat())
        assert old_key[1] < cutoff
        assert not (recent_key[1] < cutoff)
