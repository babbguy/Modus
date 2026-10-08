"""Tests for orchestrator.core.report_scheduler — schedule eval, CSV, Slack."""
from datetime import datetime, timezone
from unittest.mock import MagicMock


from orchestrator.core.report_scheduler import (
    _is_due,
    _to_csv,
    _format_slack_message,
)


# ── Mock FinanceReport ───────────────────────────────────────────────────────

def _mock_report(schedule="daily", is_active=True, last_run_at=None, report_type="chargeback"):
    r = MagicMock()
    r.schedule = schedule
    r.is_active = is_active
    r.last_run_at = last_run_at
    r.report_type = report_type
    r.name = "Test Report"
    return r


# ── _is_due ──────────────────────────────────────────────────────────────────

class TestIsDue:
    def test_inactive_not_due(self):
        r = _mock_report(is_active=False)
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is False

    def test_never_run_is_due(self):
        r = _mock_report(last_run_at=None)
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is True

    def test_daily_due(self):
        r = _mock_report(
            schedule="daily",
            last_run_at=datetime(2026, 4, 8, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 9, 1, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is True

    def test_daily_not_due(self):
        r = _mock_report(
            schedule="daily",
            last_run_at=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is False

    def test_weekly_due(self):
        # Monday is April 6, 2026
        r = _mock_report(
            schedule="weekly",
            last_run_at=datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)  # Thursday
        assert _is_due(r, now) is True

    def test_weekly_not_due(self):
        r = _mock_report(
            schedule="weekly",
            last_run_at=datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is False

    def test_monthly_due(self):
        r = _mock_report(
            schedule="monthly",
            last_run_at=datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is True

    def test_monthly_not_due(self):
        r = _mock_report(
            schedule="monthly",
            last_run_at=datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is False

    def test_quarterly_due(self):
        r = _mock_report(
            schedule="quarterly",
            last_run_at=datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)  # Q2
        assert _is_due(r, now) is True

    def test_quarterly_not_due(self):
        r = _mock_report(
            schedule="quarterly",
            last_run_at=datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)  # still Q2
        assert _is_due(r, now) is False

    def test_unknown_schedule(self):
        r = _mock_report(
            schedule="biweekly",
            last_run_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_due(r, now) is False


# ── _to_csv ──────────────────────────────────────────────────────────────────

class TestToCsv:
    def test_basic(self):
        data = [
            {"team": "alpha", "cost_usd": 100.0},
            {"team": "beta", "cost_usd": 200.0},
        ]
        csv_str = _to_csv(data)
        assert "team" in csv_str
        assert "alpha" in csv_str
        assert "200.0" in csv_str

    def test_empty(self):
        assert _to_csv([]) == ""

    def test_single_row(self):
        data = [{"key": "value"}]
        csv_str = _to_csv(data)
        assert "key" in csv_str
        assert "value" in csv_str


# ── _format_slack_message ────────────────────────────────────────────────────

class TestFormatSlackMessage:
    def test_burn_rate_format(self):
        report = _mock_report(report_type="burn_rate")
        data = [
            {
                "team": "alpha", "team_name": "Alpha Team",
                "spend_usd": 100, "budget_usd": 500,
                "burn_pct": 20.0, "risk": "on-track",
            },
            {
                "team": "beta", "team_name": "Beta Team",
                "spend_usd": 200, "budget_usd": 300,
                "burn_pct": 66.7, "risk": "over-budget",
            },
        ]
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, data, now)
        assert "text" in msg or "blocks" in msg

    def test_chargeback_format(self):
        report = _mock_report(report_type="chargeback")
        data = [
            {"team_name": "alpha", "cost_usd": 100},
            {"team_name": "beta", "cost_usd": 200},
        ]
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, data, now)
        assert "text" in msg or "blocks" in msg

    def test_empty_data(self):
        report = _mock_report(report_type="chargeback")
        now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
        msg = _format_slack_message(report, [], now)
        assert isinstance(msg, dict)
