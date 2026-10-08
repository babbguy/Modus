"""Tests for orchestrator.core.policy_engine — conditions, window, evaluation."""
import pytest
from datetime import datetime, timedelta, timezone

from orchestrator.core.policy_engine import (
    EvaluateRequest,
    PolicyResult,
    _ALLOW,
    _conditions_match,
    _window_key,
)


# ── PolicyResult ─────────────────────────────────────────────────────────────

class TestPolicyResult:
    def test_allow_property(self):
        r = PolicyResult(decision="allow", reason="ok")
        assert r.allowed is True

    def test_deny_not_allowed(self):
        r = PolicyResult(decision="deny", reason="blocked")
        assert r.allowed is False

    def test_throttle_not_allowed(self):
        r = PolicyResult(decision="throttle", reason="slow down")
        assert r.allowed is False

    def test_default_allow(self):
        assert _ALLOW.allowed is True
        assert _ALLOW.decision == "allow"


# ── EvaluateRequest ──────────────────────────────────────────────────────────

class TestEvaluateRequest:
    def test_defaults(self):
        req = EvaluateRequest(
            app_id="app1",
            team_id="team1",
            provider="anthropic",
        )
        assert req.resource_type == "llm_call"
        assert req.model is None
        assert req.estimated_tokens is None
        assert req.estimated_cost is None


# ── _window_key ──────────────────────────────────────────────────────────────

class TestWindowKey:
    def test_hourly(self):
        now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
        key, start, end = _window_key("hourly", now)
        assert key == "hourly:2026-04-09T14"
        assert start.minute == 0
        assert end - start == timedelta(hours=1)

    def test_daily(self):
        now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
        key, start, end = _window_key("daily", now)
        assert key == "daily:2026-04-09"
        assert start.hour == 0
        assert end - start == timedelta(days=1)

    def test_monthly(self):
        now = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)
        key, start, end = _window_key("monthly", now)
        assert key == "monthly:2026-04"
        assert start.day == 1
        assert end.month == 5

    def test_monthly_december(self):
        now = datetime(2026, 12, 15, 0, 0, 0, tzinfo=timezone.utc)
        key, start, end = _window_key("monthly", now)
        assert key == "monthly:2026-12"
        assert end.year == 2027
        assert end.month == 1

    def test_unknown_period(self):
        now = datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="Unknown period"):
            _window_key("yearly", now)


# ── _conditions_match ────────────────────────────────────────────────────────

class TestConditionsMatch:
    def _req(self, **kw):
        defaults = {
            "app_id": "app1",
            "team_id": "team1",
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "environment": "production",
            "resource_type": "llm_call",
        }
        defaults.update(kw)
        return EvaluateRequest(**defaults)

    def test_no_conditions(self):
        assert _conditions_match(None, self._req()) is True
        assert _conditions_match({}, self._req()) is True

    def test_providers_match(self):
        cond = {"providers": ["anthropic", "openai"]}
        assert _conditions_match(cond, self._req(provider="anthropic")) is True
        assert _conditions_match(cond, self._req(provider="google")) is False

    def test_model_pattern_match(self):
        cond = {"model_pattern": "claude-*"}
        assert _conditions_match(cond, self._req(model="claude-sonnet-4-20250514")) is True
        assert _conditions_match(cond, self._req(model="gpt-4o")) is False

    def test_model_pattern_no_model(self):
        cond = {"model_pattern": "claude-*"}
        assert _conditions_match(cond, self._req(model=None)) is False

    def test_environments_match(self):
        cond = {"environments": ["production"]}
        assert _conditions_match(cond, self._req(environment="production")) is True
        assert _conditions_match(cond, self._req(environment="staging")) is False

    def test_resource_types_match(self):
        cond = {"resource_types": ["llm_call"]}
        assert _conditions_match(cond, self._req(resource_type="llm_call")) is True
        assert _conditions_match(cond, self._req(resource_type="embedding")) is False

    def test_resource_types_default(self):
        cond = {"resource_types": ["llm_call"]}
        req = self._req(resource_type=None)
        assert _conditions_match(cond, req) is True  # defaults to "llm_call"

    def test_multiple_conditions_and(self):
        cond = {
            "providers": ["anthropic"],
            "environments": ["production"],
        }
        assert _conditions_match(cond, self._req()) is True
        assert _conditions_match(
            cond, self._req(provider="openai")
        ) is False
        assert _conditions_match(
            cond, self._req(environment="staging")
        ) is False

    def test_case_insensitive_model_pattern(self):
        cond = {"model_pattern": "GPT-*"}
        assert _conditions_match(cond, self._req(model="gpt-4o")) is True
