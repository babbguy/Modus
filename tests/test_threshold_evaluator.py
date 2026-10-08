"""
Tests for orchestrator.core.threshold_evaluator — period windows and severity checking.
"""
from __future__ import annotations

from datetime import datetime, timezone


from orchestrator.core.threshold_evaluator import (
    ALERT_TIERS,
    _fired_tier_key,
    _period_window,
    _severity_meets_min,
)


class TestPeriodWindow:
    def test_hourly(self):
        now = datetime(2026, 4, 9, 14, 35, 22, tzinfo=timezone.utc)
        start, end = _period_window("hourly", now)
        assert start == datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 4, 9, 15, 0, 0, tzinfo=timezone.utc)

    def test_daily(self):
        now = datetime(2026, 4, 9, 14, 35, 22, tzinfo=timezone.utc)
        start, end = _period_window("daily", now)
        assert start == datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc)

    def test_weekly(self):
        # 2026-04-09 is a Thursday (weekday=3)
        now = datetime(2026, 4, 9, 14, 35, 22, tzinfo=timezone.utc)
        start, end = _period_window("weekly", now)
        assert start == datetime(2026, 4, 6, 0, 0, 0, tzinfo=timezone.utc)  # Monday
        assert end == datetime(2026, 4, 13, 0, 0, 0, tzinfo=timezone.utc)

    def test_monthly(self):
        now = datetime(2026, 4, 9, 14, 35, 22, tzinfo=timezone.utc)
        start, end = _period_window("monthly", now)
        assert start == datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_monthly_december(self):
        now = datetime(2026, 12, 15, 0, 0, 0, tzinfo=timezone.utc)
        start, end = _period_window("monthly", now)
        assert start == datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class TestFiredTierKey:
    def test_returns_tuple(self):
        dt = datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
        key = _fired_tier_key("thresh-1", dt)
        assert isinstance(key, tuple)
        assert key[0] == "thresh-1"
        assert "2026-04-09" in key[1]


class TestSeverityMeetsMin:
    def test_critical_meets_all(self):
        assert _severity_meets_min("critical", "caution") is True
        assert _severity_meets_min("critical", "warning") is True
        assert _severity_meets_min("critical", "critical") is True

    def test_caution_only_meets_caution(self):
        assert _severity_meets_min("caution", "caution") is True
        assert _severity_meets_min("caution", "warning") is False
        assert _severity_meets_min("caution", "critical") is False

    def test_warning_meets_caution_and_warning(self):
        assert _severity_meets_min("warning", "caution") is True
        assert _severity_meets_min("warning", "warning") is True
        assert _severity_meets_min("warning", "critical") is False

    def test_unknown_severity(self):
        assert _severity_meets_min("unknown", "caution") is True
        assert _severity_meets_min("unknown", "warning") is False


class TestAlertTiers:
    def test_three_tiers(self):
        assert len(ALERT_TIERS) == 3

    def test_tier_ordering(self):
        pcts = [t[0] for t in ALERT_TIERS]
        assert pcts == [70, 90, 100]

    def test_tier_severities(self):
        sevs = [t[1] for t in ALERT_TIERS]
        assert sevs == ["caution", "warning", "critical"]
