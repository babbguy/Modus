"""
Conductor Push coverage.

Targets orchestrator.core.conductor_push: instance ID generation,
circuit breaker, push logic, registration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

# ═══════════════════════════════════════════════════════════════════════════════
# 1. INSTANCE ID
# ═══════════════════════════════════════════════════════════════════════════════

def test_get_instance_id_configured():
    from orchestrator.core import conductor_push as mod
    with patch.object(mod.settings, "conductor_instance_id", "my-instance"):
        assert mod._get_instance_id() == "my-instance"


def test_get_instance_id_generated():
    from orchestrator.core import conductor_push as mod
    with patch.object(mod.settings, "conductor_instance_id", ""):
        result = mod._get_instance_id()
        assert isinstance(result, str)
        assert len(result) == 16


def test_get_instance_name_configured():
    from orchestrator.core import conductor_push as mod
    with patch.object(mod.settings, "conductor_instance_name", "My Orch"):
        assert mod._get_instance_name() == "My Orch"


def test_get_instance_name_fallback():
    from orchestrator.core import conductor_push as mod
    with patch.object(mod.settings, "conductor_instance_name", ""):
        result = mod._get_instance_name()
        assert result == mod.settings.app_name


# ═══════════════════════════════════════════════════════════════════════════════
# 2. REGISTER (no conductor URL → False)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_register_no_url():
    from orchestrator.core import conductor_push as mod
    with patch.object(mod.settings, "conductor_url", ""):
        result = await mod.register_with_conductor()
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PUSH (no conductor URL → False)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_push_no_url():
    from orchestrator.core import conductor_push as mod
    with patch.object(mod.settings, "conductor_url", ""):
        result = await mod.push_to_conductor()
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════════════════════

async def test_push_circuit_breaker_open():
    from orchestrator.core import conductor_push as mod
    # Set circuit breaker to open
    mod._circuit_open_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    try:
        with patch.object(mod.settings, "conductor_url", "http://conductor:8000"):
            result = await mod.push_to_conductor()
            assert result is False
    finally:
        mod._circuit_open_until = None


async def test_push_circuit_breaker_expired():
    from orchestrator.core import conductor_push as mod
    import httpx as httpx_mod
    # Set circuit breaker to past time
    mod._circuit_open_until = datetime.now(timezone.utc) - timedelta(minutes=5)
    mod._consecutive_failures = 0
    # It will try to push but fail (no actual conductor)
    # Mock httpx.AsyncClient at the module level since it's imported inside the function
    with patch.object(mod.settings, "conductor_url", "http://conductor:8000"):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        with patch.object(httpx_mod, "AsyncClient", return_value=mock_client):
            result = await mod.push_to_conductor()
            assert result is False
    mod._circuit_open_until = None
    mod._consecutive_failures = 0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PUSH LOOP (no URL → early return)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_push_loop_no_url():
    from orchestrator.core import conductor_push as mod
    with patch.object(mod.settings, "conductor_url", ""):
        await mod.run_conductor_push_loop()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CIRCUIT BREAKER THRESHOLD CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

def test_circuit_breaker_constants():
    from orchestrator.core.conductor_push import (
        _CIRCUIT_BREAKER_THRESHOLD,
        _CIRCUIT_BREAKER_COOLDOWN,
    )
    assert _CIRCUIT_BREAKER_THRESHOLD == 5
    assert _CIRCUIT_BREAKER_COOLDOWN == 60
