"""Tests for orchestrator.core.connection_checker — URL masking, check, cache."""
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError


from orchestrator.core.connection_checker import (
    _cache,
    _cache_get,
    _cache_set,
    _check_url,
    _make_connection,
    _mask_key,
    _mask_url,
    clear_cache,
)


# ── _mask_url ────────────────────────────────────────────────────────────────

class TestMaskUrl:
    def test_standard_url(self):
        result = _mask_url("https://api.anthropic.com/v1/messages")
        assert "api.anth" in result
        assert "***" in result
        assert result.startswith("https://")

    def test_short_host(self):
        result = _mask_url("http://lo.co/x")
        assert "***" in result

    def test_empty(self):
        assert _mask_url("") == ""

    def test_localhost(self):
        result = _mask_url("http://localhost:8080")
        assert "localho" in result or "localhost" in result

    def test_very_long_url(self):
        result = _mask_url("https://very-long-hostname.example.com/path/to/resource")
        assert "***" in result


# ── _mask_key ────────────────────────────────────────────────────────────────

class TestMaskKey:
    def test_standard_key(self):
        result = _mask_key("sk-abc123456789xY7z")
        assert result.startswith("sk-")
        assert result.endswith("xY7z")
        assert "..." in result

    def test_short_key(self):
        result = _mask_key("short")
        assert "..." in result

    def test_empty(self):
        assert _mask_key("") == ""

    def test_exactly_8_chars(self):
        result = _mask_key("12345678")
        assert "..." in result


# ── _check_url ───────────────────────────────────────────────────────────────

class TestCheckUrl:
    @patch("orchestrator.core.connection_checker.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        ok, code, err = _check_url("https://example.com")
        assert ok is True
        assert code == 200
        assert err is None

    @patch("orchestrator.core.connection_checker.urlopen")
    def test_http_error_still_connected(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://example.com", 401, "Unauthorized", {}, None
        )
        ok, code, err = _check_url("https://example.com")
        assert ok is True
        assert code == 401

    @patch("orchestrator.core.connection_checker.urlopen")
    def test_url_error(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("Connection refused")
        ok, code, err = _check_url("https://example.com")
        assert ok is False
        assert code == 0
        assert err is not None

    @patch("orchestrator.core.connection_checker.urlopen")
    def test_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError()
        ok, code, err = _check_url("https://example.com")
        assert ok is False
        assert code == 0
        assert "timed out" in err.lower()

    @patch("orchestrator.core.connection_checker.urlopen")
    def test_os_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("Network unreachable")
        ok, code, err = _check_url("https://example.com")
        assert ok is False
        assert code == 0

    @patch("orchestrator.core.connection_checker.urlopen")
    def test_generic_exception(self, mock_urlopen):
        mock_urlopen.side_effect = RuntimeError("boom")
        ok, code, err = _check_url("https://example.com")
        assert ok is False
        assert "RuntimeError" in err


# ── Cache ────────────────────────────────────────────────────────────────────

class TestCache:
    def setup_method(self):
        clear_cache()

    def test_cache_set_and_get(self):
        _cache_set("test-conn", {"status": "ok"})
        result = _cache_get("test-conn")
        assert result is not None
        assert result["status"] == "ok"

    def test_cache_miss(self):
        assert _cache_get("nonexistent") is None

    def test_cache_expired(self):
        import time
        _cache["expired-conn"] = ({"status": "ok"}, time.monotonic() - 120)
        assert _cache_get("expired-conn") is None

    def test_clear_cache(self):
        _cache_set("a", {"x": 1})
        _cache_set("b", {"x": 2})
        clear_cache()
        assert _cache_get("a") is None
        assert _cache_get("b") is None


# ── _make_connection ─────────────────────────────────────────────────────────

class TestMakeConnection:
    def test_basic(self):
        conn = _make_connection(
            "test-id", "platform", "Test", "http",
            endpoint="https://api.example.com",
            status="unknown",
        )
        assert conn["id"] == "test-id"
        assert conn["category"] == "platform"
        assert conn["status"] == "unknown"
        assert "***" in conn["endpoint"]
        assert conn["_raw_url"] == "https://api.example.com"

    def test_no_endpoint(self):
        conn = _make_connection("id", "cat", "Name", "type")
        assert conn["endpoint"] == ""
        assert conn["status"] == "not_configured"

    def test_metadata(self):
        conn = _make_connection(
            "id", "cat", "Name", "type",
            metadata={"key": "value"},
        )
        assert conn["metadata"]["key"] == "value"
