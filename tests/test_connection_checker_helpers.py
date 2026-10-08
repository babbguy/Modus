"""
Connection Checker coverage.

Targets orchestrator.core.connection_checker: URL masking, key masking,
cache, _check_url, _async_check_url, _gather_* functions, _make_connection.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

# ═══════════════════════════════════════════════════════════════════════════════
# 1. URL MASKING
# ═══════════════════════════════════════════════════════════════════════════════

def test_mask_url_normal():
    from orchestrator.core.connection_checker import _mask_url
    result = _mask_url("https://api.anthropic.com/v1/messages")
    assert result.startswith("https://api.anth")
    assert "***" in result


def test_mask_url_short():
    from orchestrator.core.connection_checker import _mask_url
    result = _mask_url("http://a.b")
    assert "***" in result


def test_mask_url_empty():
    from orchestrator.core.connection_checker import _mask_url
    assert _mask_url("") == ""


def test_mask_url_localhost():
    from orchestrator.core.connection_checker import _mask_url
    result = _mask_url("http://localhost:11434")
    assert "localhos***" in result or "localhost" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. KEY MASKING
# ═══════════════════════════════════════════════════════════════════════════════

def test_mask_key_normal():
    from orchestrator.core.connection_checker import _mask_key
    result = _mask_key("sk-1234567890abcdef")
    assert result.startswith("sk-")
    assert result.endswith("cdef")
    assert "..." in result


def test_mask_key_short():
    from orchestrator.core.connection_checker import _mask_key
    result = _mask_key("abcd")
    assert "..." in result
    assert result.startswith("ab")


def test_mask_key_empty():
    from orchestrator.core.connection_checker import _mask_key
    assert _mask_key("") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 3. CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def test_cache_set_get():
    from orchestrator.core.connection_checker import _cache_set, _cache_get, clear_cache
    clear_cache()
    _cache_set("test-id", {"status": "connected"})
    result = _cache_get("test-id")
    assert result is not None
    assert result["status"] == "connected"
    clear_cache()


def test_cache_miss():
    from orchestrator.core.connection_checker import _cache_get, clear_cache
    clear_cache()
    assert _cache_get("nonexistent") is None


def test_cache_expiry():
    from orchestrator.core import connection_checker as mod
    mod.clear_cache()
    # Manually insert with old timestamp
    mod._cache["expired"] = ({"status": "ok"}, time.monotonic() - 999)
    assert mod._cache_get("expired") is None
    mod.clear_cache()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. MAKE CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════

def test_make_connection():
    from orchestrator.core.connection_checker import _make_connection
    conn = _make_connection(
        "test", "platform", "Test Service", "http",
        endpoint="https://example.com",
        status="connected",
        metadata={"note": "testing"},
    )
    assert conn["id"] == "test"
    assert conn["category"] == "platform"
    assert conn["status"] == "connected"
    assert "***" in conn["endpoint"]
    assert conn["_raw_url"] == "https://example.com"


def test_make_connection_no_endpoint():
    from orchestrator.core.connection_checker import _make_connection
    conn = _make_connection("test", "platform", "Test", "http")
    assert conn["endpoint"] == ""
    assert conn["status"] == "not_configured"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CHECK URL (mocked)
# ═══════════════════════════════════════════════════════════════════════════════

def test_check_url_success():
    from orchestrator.core.connection_checker import _check_url
    mock_resp = MagicMock()
    mock_resp.status = 200
    with patch("orchestrator.core.connection_checker.urlopen", return_value=mock_resp) as mock_open:
        mock_open.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        ok, status, err = _check_url("https://example.com")
        assert ok is True
        assert status == 200
        assert err is None


def test_check_url_http_error():
    from orchestrator.core.connection_checker import _check_url
    from urllib.error import HTTPError
    with patch("orchestrator.core.connection_checker.urlopen") as mock_open:
        mock_open.side_effect = HTTPError(
            "https://example.com", 401, "Unauthorized", {}, None,
        )
        ok, status, err = _check_url("https://example.com")
        assert ok is True  # host reachable
        assert status == 401


def test_check_url_url_error():
    from orchestrator.core.connection_checker import _check_url
    from urllib.error import URLError
    with patch("orchestrator.core.connection_checker.urlopen") as mock_open:
        mock_open.side_effect = URLError("Connection refused")
        ok, status, err = _check_url("https://example.com")
        assert ok is False
        assert status == 0
        assert err is not None


def test_check_url_timeout():
    from orchestrator.core.connection_checker import _check_url
    with patch("orchestrator.core.connection_checker.urlopen") as mock_open:
        mock_open.side_effect = TimeoutError()
        ok, status, err = _check_url("https://example.com")
        assert ok is False
        assert "timed out" in err.lower()


def test_check_url_os_error():
    from orchestrator.core.connection_checker import _check_url
    with patch("orchestrator.core.connection_checker.urlopen") as mock_open:
        mock_open.side_effect = OSError("Network unreachable")
        ok, status, err = _check_url("https://example.com")
        assert ok is False
        assert "unreachable" in err.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. ASYNC CHECK URL
# ═══════════════════════════════════════════════════════════════════════════════

async def test_async_check_url():
    from orchestrator.core.connection_checker import _async_check_url
    with patch("orchestrator.core.connection_checker._check_url") as mock_check:
        mock_check.return_value = (True, 200, None)
        ok, status, err = await _async_check_url("https://example.com")
        assert ok is True
        assert status == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 7. GATHER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_gather_notifications_empty(monkeypatch):
    # No configured channels -> no connections. Reads the real DB config now,
    # not phantom settings.
    from orchestrator.core.connection_checker import _gather_notifications
    monkeypatch.setattr(
        "orchestrator.api.notifications._load_config",
        AsyncMock(return_value={}),
    )
    # Ensure a session factory exists for the function to proceed.
    from orchestrator.db import session as session_mod
    if session_mod._session_factory is None:
        conns = await _gather_notifications()
        assert conns == []
    else:
        conns = await _gather_notifications()
        assert conns == []


async def test_gather_notifications_slack(monkeypatch):
    # A Slack channel configured in the DB appears on the connections page.
    from orchestrator.core import connection_checker as cc
    from orchestrator.db import session as session_mod
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from orchestrator.db.models import Base

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "_session_factory", factory)
    monkeypatch.setattr(cc, "_session_factory", factory) if hasattr(cc, "_session_factory") else None
    monkeypatch.setattr(
        "orchestrator.api.notifications._load_config",
        AsyncMock(return_value={
            "slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/xxx"},
        }),
    )
    conns = await cc._gather_notifications()
    assert len(conns) == 1
    assert conns[0]["type"] == "slack"
    assert conns[0]["status"] == "configured"
    await eng.dispose()


def test_gather_ai_providers_none():
    from orchestrator.core.connection_checker import _gather_ai_providers
    with patch("orchestrator.core.connection_checker.settings") as mock_s:
        mock_s.assistant_provider = ""
        mock_s.assistant_api_key = ""
        mock_s.summary_agent = ""
        mock_s.summary_api_key = None
        mock_s.summary_base_url = ""
        mock_s.summary_model_id = ""
        conns = _gather_ai_providers()
        assert conns == []


