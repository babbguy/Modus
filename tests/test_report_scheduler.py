"""
Tests for orchestrator.core.report_scheduler — Schedule evaluation, CSV
generation, Slack formatting, and delivery helpers.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone


from orchestrator.core.report_scheduler import (
    _format_slack_message,
    _is_due,
    _to_csv,
    _GENERATORS,
)


# ── Fake FinanceReport for testing ───────────────────────────────────────────


class FakeReport:
    def __init__(self, schedule, is_active=True, last_run_at=None,
                 report_type="chargeback", name="Test Report"):
        self.schedule = schedule
        self.is_active = is_active
        self.last_run_at = last_run_at
        self.report_type = report_type
        self.name = name


# ── _is_due ──────────────────────────────────────────────────────────────────


class TestIsDue:
    def test_inactive_report_never_due(self):
        report = FakeReport("daily", is_active=False)
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(report, now) is False

    def test_never_run_is_always_due(self):
        report = FakeReport("daily", last_run_at=None)
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(report, now) is True

    def test_daily_ran_today_not_due(self):
        now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
        report = FakeReport("daily", last_run_at=last_run)
        assert _is_due(report, now) is False

    def test_daily_ran_yesterday_is_due(self):
        now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 4, 8, 8, 0, 0, tzinfo=timezone.utc)
        report = FakeReport("daily", last_run_at=last_run)
        assert _is_due(report, now) is True

    def test_weekly_ran_this_week_not_due(self):
        # April 9, 2026 is Thursday. Monday is April 6.
        now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 4, 7, 8, 0, 0, tzinfo=timezone.utc)  # Tuesday
        report = FakeReport("weekly", last_run_at=last_run)
        assert _is_due(report, now) is False

    def test_weekly_ran_last_week_is_due(self):
        now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 4, 1, 8, 0, 0, tzinfo=timezone.utc)  # Previous week
        report = FakeReport("weekly", last_run_at=last_run)
        assert _is_due(report, now) is True

    def test_monthly_ran_this_month_not_due(self):
        now = datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 4, 2, 8, 0, 0, tzinfo=timezone.utc)
        report = FakeReport("monthly", last_run_at=last_run)
        assert _is_due(report, now) is False

    def test_monthly_ran_last_month_is_due(self):
        now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 3, 28, 8, 0, 0, tzinfo=timezone.utc)
        report = FakeReport("monthly", last_run_at=last_run)
        assert _is_due(report, now) is True

    def test_quarterly_q1_not_due(self):
        now = datetime(2026, 2, 15, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)
        report = FakeReport("quarterly", last_run_at=last_run)
        assert _is_due(report, now) is False

    def test_quarterly_new_quarter_is_due(self):
        now = datetime(2026, 4, 5, 14, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 3, 15, 8, 0, 0, tzinfo=timezone.utc)
        report = FakeReport("quarterly", last_run_at=last_run)
        assert _is_due(report, now) is True

    def test_unknown_schedule_not_due(self):
        report = FakeReport("hourly", last_run_at=datetime(2026, 4, 9, tzinfo=timezone.utc))
        now = datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_due(report, now) is False


# ── _to_csv ──────────────────────────────────────────────────────────────────


class TestToCSV:
    def test_empty_data(self):
        assert _to_csv([]) == ""

    def test_single_row(self):
        data = [{"team": "eng", "cost": 100.50}]
        result = _to_csv(data)
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["team"] == "eng"
        assert rows[0]["cost"] == "100.5"

    def test_multiple_rows(self):
        data = [
            {"team": "eng", "cost": 100},
            {"team": "marketing", "cost": 200},
        ]
        result = _to_csv(data)
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert len(rows) == 2

    def test_header_matches_keys(self):
        data = [{"a": 1, "b": 2, "c": 3}]
        result = _to_csv(data)
        header = result.split("\n")[0]
        assert "a" in header
        assert "b" in header
        assert "c" in header


# ── Slack message formatting ─────────────────────────────────────────────────


class TestFormatSlackMessage:
    def test_chargeback_report(self):
        report = FakeReport("daily", report_type="chargeback", name="Daily Chargeback")
        data = [
            {"team_name": "eng", "cost_usd": 500},
            {"team_name": "ml", "cost_usd": 300},
        ]
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, data, now)
        assert "blocks" in msg
        assert len(msg["blocks"]) >= 2
        # Header should have report name
        header_text = msg["blocks"][0]["text"]["text"]
        assert "Daily Chargeback" in header_text

    def test_burn_rate_report(self):
        report = FakeReport("weekly", report_type="burn_rate", name="Weekly Burn")
        data = [
            {"team_name": "eng", "spend_usd": 500, "budget_usd": 1000,
             "burn_pct": 50, "risk": "on-track"},
            {"team_name": "ml", "spend_usd": 900, "budget_usd": 1000,
             "burn_pct": 90, "risk": "at-risk"},
        ]
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, data, now)
        body_text = msg["blocks"][1]["text"]["text"]
        assert "2 teams tracked" in body_text
        assert "1 at risk" in body_text

    def test_forecast_report(self):
        report = FakeReport("monthly", report_type="forecast", name="Monthly Forecast")
        data = [
            {"team_name": "eng", "forecast_eom_usd": 1500, "breach_predicted": True,
             "breach_date": "2026-04-25"},
            {"team_name": "ml", "forecast_eom_usd": 500, "breach_predicted": False},
        ]
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, data, now)
        body_text = msg["blocks"][1]["text"]["text"]
        assert "2 teams forecasted" in body_text
        assert "1 breach" in body_text

    def test_variance_report(self):
        report = FakeReport("monthly", report_type="variance", name="Variance")
        data = [
            {"team_name": "eng", "delta_usd": 200, "delta_pct": 15.0},
            {"team_name": "ml", "delta_usd": -100, "delta_pct": -8.0},
        ]
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, data, now)
        assert "blocks" in msg

    def test_context_block(self):
        report = FakeReport("daily", report_type="chargeback", name="Test")
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, [{"cost_usd": 100}], now)
        ctx = msg["blocks"][-1]
        assert ctx["type"] == "context"
        assert "2026-04-09" in ctx["elements"][0]["text"]


# ── Generators registry ──────────────────────────────────────────────────────


class TestGeneratorsRegistry:
    def test_all_types_registered(self):
        expected = {"chargeback", "burn_rate", "variance", "audit_trail", "forecast"}
        assert set(_GENERATORS.keys()) == expected

    def test_generators_are_callable(self):
        for name, gen in _GENERATORS.items():
            assert callable(gen), f"Generator '{name}' is not callable"
