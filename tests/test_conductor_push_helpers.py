"""Tests for orchestrator.core.conductor_push — circuit breaker, helpers."""
from unittest.mock import patch


from orchestrator.core.conductor_push import (
    _CIRCUIT_BREAKER_COOLDOWN,
    _CIRCUIT_BREAKER_THRESHOLD,
    _get_instance_id,
    _get_instance_name,
)


# ── _get_instance_id ─────────────────────────────────────────────────────────

class TestGetInstanceId:
    def test_from_settings(self):
        with patch("orchestrator.core.conductor_push.settings") as mock_s:
            mock_s.conductor_instance_id = "custom-id-123"
            result = _get_instance_id()
            assert result == "custom-id-123"

    def test_generated_from_config(self):
        with patch("orchestrator.core.conductor_push.settings") as mock_s:
            mock_s.conductor_instance_id = ""
            mock_s.app_name = "modus"
            mock_s.database_url = "postgresql://localhost/modus"
            result = _get_instance_id()
            assert len(result) == 16
            assert isinstance(result, str)

    def test_deterministic(self):
        with patch("orchestrator.core.conductor_push.settings") as mock_s:
            mock_s.conductor_instance_id = ""
            mock_s.app_name = "modus"
            mock_s.database_url = "postgresql://localhost/modus"
            r1 = _get_instance_id()
            r2 = _get_instance_id()
            assert r1 == r2


# ── _get_instance_name ───────────────────────────────────────────────────────

class TestGetInstanceName:
    def test_from_settings(self):
        with patch("orchestrator.core.conductor_push.settings") as mock_s:
            mock_s.conductor_instance_name = "My Instance"
            result = _get_instance_name()
            assert result == "My Instance"

    def test_fallback_to_app_name(self):
        with patch("orchestrator.core.conductor_push.settings") as mock_s:
            mock_s.conductor_instance_name = ""
            mock_s.app_name = "modus-prod"
            result = _get_instance_name()
            assert result == "modus-prod"


# ── Constants ────────────────────────────────────────────────────────────────

class TestConstants:
    def test_circuit_breaker_threshold(self):
        assert _CIRCUIT_BREAKER_THRESHOLD == 5

    def test_circuit_breaker_cooldown(self):
        assert _CIRCUIT_BREAKER_COOLDOWN == 60
