"""
Tests for orchestrator.core.conductor_push — data push, circuit breaker, retry logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import orchestrator.core.conductor_push as cp
from orchestrator.core.conductor_push import (
    _CIRCUIT_BREAKER_COOLDOWN,
    _CIRCUIT_BREAKER_THRESHOLD,
    _get_instance_id,
    _get_instance_name,
    push_to_conductor,
)


# ── Instance ID / Name ──────────────────────────────────────────────────────


class TestInstanceId:
    def test_deterministic(self):
        with patch.object(cp.settings, "conductor_instance_id", "my-id"):
            assert _get_instance_id() == "my-id"

    def test_generated_from_app_name_and_db(self):
        with patch.object(cp.settings, "conductor_instance_id", ""):
            with patch.object(cp.settings, "app_name", "test-app"):
                with patch.object(cp.settings, "database_url", "sqlite:///test.db"):
                    result = _get_instance_id()
                    assert len(result) == 16
                    # Deterministic
                    assert _get_instance_id() == result


class TestInstanceName:
    def test_explicit_name(self):
        with patch.object(cp.settings, "conductor_instance_name", "My Orchestrator"):
            assert _get_instance_name() == "My Orchestrator"

    def test_fallback_to_app_name(self):
        with patch.object(cp.settings, "conductor_instance_name", ""):
            with patch.object(cp.settings, "app_name", "modus"):
                assert _get_instance_name() == "modus"


# ── Circuit breaker ──────────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_constants(self):
        assert _CIRCUIT_BREAKER_THRESHOLD == 5
        assert _CIRCUIT_BREAKER_COOLDOWN == 60

    @pytest.mark.asyncio
    async def test_no_conductor_url_returns_false(self):
        with patch.object(cp.settings, "conductor_url", ""):
            result = await push_to_conductor()
            assert result is False

    @pytest.mark.asyncio
    async def test_circuit_open_skips_push(self):
        old_until = cp._circuit_open_until
        try:
            cp._circuit_open_until = datetime.now(timezone.utc) + timedelta(minutes=5)
            with patch.object(cp.settings, "conductor_url", "http://conductor:8000"):
                result = await push_to_conductor()
                assert result is False
        finally:
            cp._circuit_open_until = old_until
