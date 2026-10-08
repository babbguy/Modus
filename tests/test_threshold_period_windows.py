"""Tests for orchestrator.core.threshold_evaluator — period windows, tiers."""
from datetime import datetime, timedelta, timezone


from orchestrator.core.threshold_evaluator import (
    ALERT_TIERS,
    _fired_tier_key,
    _period_window,
)


# ── _period_window ───────────────────────────────────────────────────────────

class TestPeriodWindow:
    def test_hourly(self):
        now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
        start, end = _period_window("hourly", now)
        assert start.hour == 14
        assert start.minute == 0
        assert end - start == timedelta(hours=1)

    def test_daily(self):
        now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
        start, end = _period_window("daily", now)
        assert start.hour == 0
        assert end - start == timedelta(days=1)

    def test_weekly(self):
        # April 9, 2026 is Thursday, Monday is April 6
        now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
        start, end = _period_window("weekly", now)
        assert start.weekday() == 0  # Monday
        assert end - start == timedelta(weeks=1)

    def test_monthly(self):
        now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
        start, end = _period_window("monthly", now)
        assert start.day == 1
        assert end.month == 5

    def test_monthly_december(self):
        now = datetime(2026, 12, 15, 12, 0, 0, tzinfo=timezone.utc)
        start, end = _period_window("monthly", now)
        assert start.month == 12
        assert end.year == 2027
        assert end.month == 1

    def test_unknown_defaults_monthly(self):
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        start, end = _period_window("unknown", now)
        assert start.day == 1


# ── _fired_tier_key ──────────────────────────────────────────────────────────

class TestFiredTierKey:
    def test_returns_tuple(self):
        ts = datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
        key = _fired_tier_key("threshold-123", ts)
        assert isinstance(key, tuple)
        assert len(key) == 2
        assert key[0] == "threshold-123"

    def test_deterministic(self):
        ts = datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
        k1 = _fired_tier_key("t1", ts)
        k2 = _fired_tier_key("t1", ts)
        assert k1 == k2


# ── ALERT_TIERS ──────────────────────────────────────────────────────────────

class TestAlertTiers:
    def test_tier_count(self):
        assert len(ALERT_TIERS) == 3

    def test_tier_ordering(self):
        pcts = [t[0] for t in ALERT_TIERS]
        assert pcts == sorted(pcts)

    def test_tier_severities(self):
        severities = [t[1] for t in ALERT_TIERS]
        assert "caution" in severities
        assert "warning" in severities
        assert "critical" in severities
