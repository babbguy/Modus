"""
Tests — SDK CLI (__main__.py)
Covers: color helpers, _get/_post, argument parsing, _policy_diff,
        _load_yaml, _dump_yaml, policy_validate, main dispatch.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest.mock import patch

import pytest


# ── Color helpers ────────────────────────────────────────────────────────────

def test_color_helper_tty():
    from modus.__main__ import _c
    with patch.object(sys.stdout, "isatty", return_value=True):
        result = _c("32", "hello")
        assert "\033[32m" in result
        assert "hello" in result


def test_color_helper_no_tty():
    from modus.__main__ import _c
    with patch.object(sys.stdout, "isatty", return_value=False):
        result = _c("32", "hello")
        assert "\033[" not in result
        assert result == "hello"


def test_ok_helper():
    from modus.__main__ import ok
    with patch.object(sys.stdout, "isatty", return_value=False):
        result = ok("test message")
        assert "test message" in result


def test_warn_helper():
    from modus.__main__ import warn
    with patch.object(sys.stdout, "isatty", return_value=False):
        result = warn("warning")
        assert "warning" in result


def test_fail_helper():
    from modus.__main__ import fail
    with patch.object(sys.stdout, "isatty", return_value=False):
        result = fail("error")
        assert "error" in result


def test_info_helper():
    from modus.__main__ import info
    with patch.object(sys.stdout, "isatty", return_value=False):
        result = info("info msg")
        assert "info msg" in result


def test_head_helper():
    from modus.__main__ import head
    with patch.object(sys.stdout, "isatty", return_value=False):
        result = head("header")
        assert "header" in result


# ── Provider detection ───────────────────────────────────────────────────────

def test_providers_list():
    from modus.__main__ import PROVIDERS
    assert len(PROVIDERS) >= 5
    labels = [p[0] for p in PROVIDERS]
    assert "anthropic" in labels
    assert "openai" in labels


def test_check_providers():
    from modus.__main__ import _check_providers
    with patch.object(sys.stdout, "isatty", return_value=False):
        found = _check_providers()
    assert isinstance(found, list)


# ── _policy_diff ─────────────────────────────────────────────────────────────

def test_policy_diff_no_changes():
    from modus.__main__ import _policy_diff
    desired = {"type": "budget_cap", "scope": "team", "effect": "deny"}
    current = {"type": "budget_cap", "scope": "team", "effect": "deny",
               "priority": 100, "is_active": True}
    diffs = _policy_diff(desired, current, {})
    assert len(diffs) == 0


def test_policy_diff_omitted_fields_use_server_defaults():
    from modus.__main__ import _policy_diff
    current = {"policy_type": "budget_cap", "scope": "team", "effect": "deny",
               "priority": 100, "is_active": True}
    assert _policy_diff({"type": "budget_cap"}, current, {}) == []
    changed = _policy_diff({"type": "budget_cap", "effect": "warn"}, current, {})
    assert any("effect" in d for d in changed)


def test_policy_diff_type_changed():
    from modus.__main__ import _policy_diff
    desired = {"type": "rate_limit"}
    current = {"type": "budget_cap"}
    diffs = _policy_diff(desired, current, {})
    assert any("type" in d for d in diffs)


def test_policy_diff_enabled_changed():
    from modus.__main__ import _policy_diff
    desired = {"enabled": False}
    current = {"is_active": True}
    diffs = _policy_diff(desired, current, {})
    assert any("enabled" in d for d in diffs)


def test_policy_diff_uses_defaults():
    from modus.__main__ import _policy_diff
    desired = {}
    current = {"scope": "team"}
    defaults = {"scope": "team"}
    diffs = _policy_diff(desired, current, defaults)
    # scope from defaults matches current, no diff
    assert not any("scope" in d for d in diffs)


# ── _load_yaml / _dump_yaml ─────────────────────────────────────────────────

def test_load_yaml_json_fallback():
    from modus.__main__ import _load_yaml
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"version": "1", "policies": []}, f)
        f.flush()
        path = f.name
    try:
        result = _load_yaml(path)
        assert result["version"] == "1"
    finally:
        os.unlink(path)


def test_dump_yaml_json_fallback():
    from modus.__main__ import _dump_yaml
    result = _dump_yaml({"key": "value"})
    # Should be valid JSON or YAML
    assert "key" in result


# ── policy_validate ──────────────────────────────────────────────────────────

def test_policy_validate_valid_file():
    from modus.__main__ import policy_validate
    data = {"version": "1", "policies": [
        {"name": "Test", "type": "budget_cap", "scope": "team", "effect": "deny",
         "team": "platform", "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name
    try:
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_validate(path)
        assert result == 0
    finally:
        os.unlink(path)


def test_policy_validate_invalid_file():
    from modus.__main__ import policy_validate
    data = {"not_valid": True}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name
    try:
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_validate(path)
        assert result == 1
    finally:
        os.unlink(path)


# ── Main dispatch ────────────────────────────────────────────────────────────

def test_main_no_args():
    from modus.__main__ import main
    with patch("sys.argv", ["modus"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


def test_main_policy_no_subcommand():
    from modus.__main__ import main
    with patch("sys.argv", ["modus", "policy"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


# ── _get and _post helpers ───────────────────────────────────────────────────

def test_get_connection_error():
    from modus.__main__ import _get
    with pytest.raises(ConnectionError):
        _get("http://localhost:1/nonexistent", headers={}, timeout=0.5)


def test_post_connection_error():
    from modus.__main__ import _post
    with pytest.raises(ConnectionError):
        _post("http://localhost:1/nonexistent", payload={"a": 1}, headers={}, timeout=0.5)


# ── _get / _post — mocked success & HTTP error paths ────────────────────────

from io import BytesIO
from unittest.mock import MagicMock


def test_get_success():
    from modus.__main__ import _get
    resp_body = json.dumps({"status": "ok"}).encode()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = resp_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("modus.__main__.urllib_request.urlopen", return_value=mock_resp):
        status, body = _get("http://localhost/health", {})

    assert status == 200
    assert body["status"] == "ok"


def test_get_http_error():
    from modus.__main__ import _get
    from urllib.error import HTTPError

    err = HTTPError("http://test", 404, "Not Found", {}, BytesIO(b'{"detail": "not found"}'))
    with patch("modus.__main__.urllib_request.urlopen", side_effect=err):
        status, body = _get("http://test", {})

    assert status == 404
    assert body.get("detail") == "not found"


def test_get_http_error_bad_body():
    from modus.__main__ import _get
    from urllib.error import HTTPError

    err = HTTPError("http://test", 500, "Error", {}, BytesIO(b"not json"))
    with patch("modus.__main__.urllib_request.urlopen", side_effect=err):
        status, body = _get("http://test", {})

    assert status == 500
    assert body == {}


def test_post_success():
    from modus.__main__ import _post
    resp_body = json.dumps({"accepted": 1}).encode()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = resp_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("modus.__main__.urllib_request.urlopen", return_value=mock_resp):
        status, body = _post("http://localhost/api", {"data": "test"}, {})

    assert status == 200
    assert body["accepted"] == 1


def test_post_http_error():
    from modus.__main__ import _post
    from urllib.error import HTTPError

    err = HTTPError("http://test", 401, "Unauthorized", {}, BytesIO(b'{"detail": "bad token"}'))
    with patch("modus.__main__.urllib_request.urlopen", side_effect=err):
        status, body = _post("http://test", {}, {})

    assert status == 401
    assert body.get("detail") == "bad token"


def test_post_http_error_bad_body():
    from modus.__main__ import _post
    from urllib.error import HTTPError

    err = HTTPError("http://test", 500, "Error", {}, BytesIO(b"not json"))
    with patch("modus.__main__.urllib_request.urlopen", side_effect=err):
        status, body = _post("http://test", {}, {})

    assert status == 500
    assert body == {}


# ── diagnose — full diagnostic paths ────────────────────────────────────────

def test_diagnose_no_url_no_token():
    from modus.__main__ import diagnose
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MODUS_URL", None)
        os.environ.pop("MODUS_ORCHESTRATOR_URL", None)
        os.environ.pop("MODUS_TEAM_TOKEN", None)
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = diagnose(url="", token="")

    assert result == 1


def test_diagnose_success_path():
    from modus.__main__ import diagnose
    mock_get = MagicMock(return_value=(200, {"service": "modus", "version": "1.0.0"}))
    mock_post = MagicMock(return_value=(200, {
        "registered": True, "team_slug": "test-team", "app_id": "diag-test",
    }))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    assert result == 0


def test_diagnose_health_non_200():
    from modus.__main__ import diagnose
    mock_get = MagicMock(return_value=(503, {}))
    mock_post = MagicMock(return_value=(200, {
        "registered": True, "team_slug": "t", "app_id": "a",
    }))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    assert result == 0


def test_diagnose_health_unreachable():
    from modus.__main__ import diagnose
    mock_get = MagicMock(side_effect=ConnectionError("refused"))
    mock_post = MagicMock(side_effect=ConnectionError("refused"))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    assert result == 1


def test_diagnose_registration_401():
    from modus.__main__ import diagnose
    mock_get = MagicMock(return_value=(200, {"service": "modus", "version": "1"}))
    mock_post = MagicMock(return_value=(401, {}))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    assert result == 1


def test_diagnose_registration_403():
    from modus.__main__ import diagnose
    mock_get = MagicMock(return_value=(200, {"service": "modus", "version": "1"}))
    mock_post = MagicMock(return_value=(403, {}))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    assert result == 1


def test_diagnose_registration_other_status():
    from modus.__main__ import diagnose
    mock_get = MagicMock(return_value=(200, {"service": "modus", "version": "1"}))
    mock_post = MagicMock(return_value=(422, {"detail": "bad payload"}))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    # Non-401/403 registration status doesn't add to errors list
    assert result == 0


def test_diagnose_registration_network_error():
    from modus.__main__ import diagnose
    mock_get = MagicMock(return_value=(200, {"service": "modus", "version": "1"}))
    mock_post = MagicMock(side_effect=ConnectionError("refused"))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    assert result == 1


def test_diagnose_disabled_env(capsys):
    from modus.__main__ import diagnose
    with patch.dict(os.environ, {"MODUS_DISABLED": "true"}):
        with patch.object(sys.stdout, "isatty", return_value=False):
            diagnose(url="", token="")

    captured = capsys.readouterr()
    assert "MODUS_DISABLED" in captured.out


def test_diagnose_reconnect_path():
    """Registration returns registered=false (reconnected)."""
    from modus.__main__ import diagnose
    mock_get = MagicMock(return_value=(200, {"service": "modus", "version": "1"}))
    mock_post = MagicMock(return_value=(200, {
        "registered": False, "team_slug": "team", "app_id": "app",
    }))

    with patch("modus.__main__._get", mock_get):
        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = diagnose(
                    url="http://localhost:9999",
                    token="mds_team_test_12345678901234567890",
                )

    assert result == 0


# ── policy_plan ──────────────────────────────────────────────────────────────

def test_policy_plan_no_changes():
    from modus.__main__ import policy_plan
    data = {"version": "1", "policies": [
        {"name": "existing", "type": "budget_cap", "effect": "deny",
         "scope": "team", "team": "platform",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        mock_get = MagicMock(return_value=(200, {
            "policies": [
                {"name": "existing", "type": "budget_cap", "effect": "deny",
                 "scope": "team", "config": {"cap_usd": "100", "period": "daily"},
                 "is_active": True},
            ]
        }))

        with patch("modus.__main__._get", mock_get):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_plan(path, url="http://test", token="tok")

        assert result == 0
    finally:
        os.unlink(path)


def test_policy_plan_with_adds_and_removes(capsys):
    from modus.__main__ import policy_plan
    data = {"version": "1", "policies": [
        {"name": "new-policy", "type": "rate_limit", "scope": "team",
         "effect": "throttle", "team": "dev",
         "config": {"max_calls": 100, "window_seconds": 60}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        mock_get = MagicMock(return_value=(200, {
            "policies": [
                {"name": "old-policy", "type": "budget_cap", "effect": "deny"},
            ]
        }))

        with patch("modus.__main__._get", mock_get):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_plan(path, url="http://test", token="tok")

        assert result == 0
        captured = capsys.readouterr()
        assert "add" in captured.out.lower() or "+" in captured.out
        assert "remove" in captured.out.lower() or "-" in captured.out
    finally:
        os.unlink(path)


def test_policy_plan_with_changes(capsys):
    from modus.__main__ import policy_plan
    data = {"version": "1", "policies": [
        {"name": "p1", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "200", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        mock_get = MagicMock(return_value=(200, {
            "policies": [
                {"name": "p1", "type": "budget_cap", "effect": "deny",
                 "scope": "team", "config": {"cap_usd": "100", "period": "daily"},
                 "is_active": True},
            ]
        }))

        with patch("modus.__main__._get", mock_get):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_plan(path, url="http://test", token="tok")

        assert result == 0
        captured = capsys.readouterr()
        assert "update" in captured.out.lower() or "~" in captured.out
    finally:
        os.unlink(path)


def test_policy_plan_no_url():
    from modus.__main__ import policy_plan
    data = {"version": "1", "policies": []}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MODUS_URL", None)
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_plan(path, url="", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_plan_validation_errors():
    from modus.__main__ import policy_plan
    data = {"not_valid": True}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_plan(path, url="http://test", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_plan_network_error():
    from modus.__main__ import policy_plan
    data = {"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch("modus.__main__._get", side_effect=ConnectionError("refused")):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_plan(path, url="http://test", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_plan_non_200():
    from modus.__main__ import policy_plan
    data = {"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch("modus.__main__._get", return_value=(500, {"detail": "err"})):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_plan(path, url="http://test", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_plan_list_response_format():
    """Handle GET /policies returning a flat list (not wrapped in {policies: []})."""
    from modus.__main__ import policy_plan
    data = {"version": "1", "policies": [
        {"name": "p1", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        # Return a list directly instead of {policies: [...]}
        mock_get = MagicMock(return_value=(200, [
            {"name": "p1", "type": "budget_cap", "effect": "deny",
             "scope": "team", "config": {"cap_usd": "100", "period": "daily"},
             "is_active": True},
        ]))

        with patch("modus.__main__._get", mock_get):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_plan(path, url="http://test", token="tok")

        assert result == 0
    finally:
        os.unlink(path)


# ── policy_apply ─────────────────────────────────────────────────────────────

def test_policy_apply_success():
    from modus.__main__ import policy_apply
    data = {"version": "1", "policies": [
        {"name": "test", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        mock_post = MagicMock(return_value=(200, {
            "summary": {"created": 1, "updated": 0, "removed": 0},
            "errors": [],
        }))

        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_apply(path, url="http://test", token="tok")

        assert result == 0
    finally:
        os.unlink(path)


def test_policy_apply_dry_run(capsys):
    from modus.__main__ import policy_apply
    data = {"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        mock_post = MagicMock(return_value=(200, {
            "summary": {"created": 0, "updated": 0, "removed": 0},
            "errors": [],
        }))

        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_apply(path, url="http://test", token="tok", dry_run=True)

        assert result == 0
        captured = capsys.readouterr()
        assert "ry run" in captured.out.lower() or "Dry" in captured.out
    finally:
        os.unlink(path)


def test_policy_apply_validation_errors():
    from modus.__main__ import policy_apply
    data = {"not_valid": True}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_apply(path, url="http://test", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_apply_no_url():
    from modus.__main__ import policy_apply
    data = {"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MODUS_URL", None)
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_apply(path, url="", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_apply_network_error():
    from modus.__main__ import policy_apply
    data = {"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch("modus.__main__._post", side_effect=ConnectionError("refused")):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_apply(path, url="http://test", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_apply_server_error():
    from modus.__main__ import policy_apply
    data = {"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        with patch("modus.__main__._post", return_value=(500, {"detail": "error"})):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_apply(path, url="http://test", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


def test_policy_apply_with_response_errors():
    from modus.__main__ import policy_apply
    data = {"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "scope": "team",
         "effect": "deny", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        path = f.name

    try:
        mock_post = MagicMock(return_value=(200, {
            "summary": {"created": 1, "updated": 0, "removed": 0},
            "errors": ["Conflict on policy X"],
        }))

        with patch("modus.__main__._post", mock_post):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_apply(path, url="http://test", token="tok")

        assert result == 1
    finally:
        os.unlink(path)


# ── policy_export ────────────────────────────────────────────────────────────

def test_policy_export_success():
    from modus.__main__ import policy_export
    mock_get = MagicMock(return_value=(200, {
        "version": "1", "policies": [{"name": "test"}],
    }))

    with patch("modus.__main__._get", mock_get):
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_export(url="http://test", token="tok")

    assert result == 0


def test_policy_export_no_url():
    from modus.__main__ import policy_export
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MODUS_URL", None)
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_export(url="", token="tok")

    assert result == 1


def test_policy_export_network_error():
    from modus.__main__ import policy_export
    with patch("modus.__main__._get", side_effect=ConnectionError("refused")):
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_export(url="http://test", token="tok")

    assert result == 1


def test_policy_export_server_error():
    from modus.__main__ import policy_export
    with patch("modus.__main__._get", return_value=(500, {"detail": "error"})):
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_export(url="http://test", token="tok")

    assert result == 1


def test_policy_export_with_scope():
    from modus.__main__ import policy_export
    mock_get = MagicMock(return_value=(200, {"policies": []}))

    with patch("modus.__main__._get", mock_get):
        with patch.object(sys.stdout, "isatty", return_value=False):
            result = policy_export(url="http://test", token="tok", scope="team")

    assert result == 0
    call_args = mock_get.call_args
    assert "scope=team" in call_args[0][0]


def test_policy_export_uses_api_key_header():
    from modus.__main__ import policy_export
    mock_get = MagicMock(return_value=(200, {"policies": []}))

    with patch.dict(os.environ, {"MODUS_MASTER_API_KEY": "master-key"}, clear=False):
        with patch("modus.__main__._get", mock_get):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = policy_export(url="http://test")

    assert result == 0
    headers = mock_get.call_args[0][1]
    assert headers.get("X-Modus-APIKey") == "master-key"
    assert "Authorization" not in headers
    assert "X-Team-Token" not in headers


def test_policy_export_token_is_sent_as_bearer_jwt():
    from modus.__main__ import policy_export
    mock_get = MagicMock(return_value=(200, {"policies": []}))

    with patch.dict(os.environ, {"MODUS_MASTER_API_KEY": ""}, clear=False):
        with patch("modus.__main__._get", mock_get):
            with patch.object(sys.stdout, "isatty", return_value=False):
                assert policy_export(url="http://test", token="a.b.c") == 0

    headers = mock_get.call_args[0][1]
    assert headers == {"Authorization": "Bearer a.b.c"}


def test_policy_plan_and_apply_send_master_key_header(tmp_path):
    import json as _json
    from modus.__main__ import policy_apply, policy_plan

    f = tmp_path / "p.json"
    f.write_text(_json.dumps({"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}))
    mock_get = MagicMock(return_value=(200, []))
    mock_post = MagicMock(return_value=(200, {"summary": {}, "errors": []}))

    with patch.dict(os.environ, {"MODUS_MASTER_API_KEY": "mk"}, clear=False):
        with patch("modus.__main__._get", mock_get), patch("modus.__main__._post", mock_post):
            assert policy_plan(str(f), url="http://test") == 0
            assert policy_apply(str(f), url="http://test") == 0

    assert mock_get.call_args[0][1] == {"X-Modus-APIKey": "mk"}
    assert mock_post.call_args[0][2] == {"X-Modus-APIKey": "mk"}


# ── main dispatch — additional commands ──────────────────────────────────────

def test_main_diagnose_command():
    from modus.__main__ import main
    with patch("sys.argv", ["modus", "diagnose", "--url", "http://test"]):
        with patch("modus.__main__.diagnose", return_value=0) as mock_diag:
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
            mock_diag.assert_called_once()


def test_main_policy_validate():
    from modus.__main__ import main
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"version": "1", "policies": []}, f)
        f.flush()
        path = f.name

    try:
        with patch("sys.argv", ["modus", "policy", "validate", path]):
            with patch("modus.__main__.policy_validate", return_value=0):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
    finally:
        os.unlink(path)


def test_main_policy_plan():
    from modus.__main__ import main
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"version": "1", "policies": []}, f)
        f.flush()
        path = f.name

    try:
        with patch("sys.argv", ["modus", "policy", "plan", path]):
            with patch("modus.__main__.policy_plan", return_value=0):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
    finally:
        os.unlink(path)


def test_main_policy_apply():
    from modus.__main__ import main
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"version": "1", "policies": []}, f)
        f.flush()
        path = f.name

    try:
        with patch("sys.argv", ["modus", "policy", "apply", path]):
            with patch("modus.__main__.policy_apply", return_value=0):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
    finally:
        os.unlink(path)


def test_main_policy_apply_dry_run():
    from modus.__main__ import main
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"version": "1", "policies": []}, f)
        f.flush()
        path = f.name

    try:
        with patch("sys.argv", ["modus", "policy", "apply", path, "--dry-run"]):
            with patch("modus.__main__.policy_apply", return_value=0):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
    finally:
        os.unlink(path)


def test_main_policy_export():
    from modus.__main__ import main
    with patch("sys.argv", ["modus", "policy", "export"]):
        with patch("modus.__main__.policy_export", return_value=0):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0


# ── _load_yaml edge cases ───────────────────────────────────────────────────

def test_load_yaml_non_dict_json_exits():
    from modus.__main__ import _load_yaml
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
        f.flush()
        path = f.name

    try:
        with patch.dict("sys.modules", {"yaml": None}):
            with patch.object(sys.stdout, "isatty", return_value=False):
                with pytest.raises(SystemExit):
                    _load_yaml(path)
    finally:
        os.unlink(path)


def test_load_yaml_invalid_content_exits():
    from modus.__main__ import _load_yaml
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write("key: value\nnested:\n  - item")
        f.flush()
        path = f.name

    try:
        with patch.dict("sys.modules", {"yaml": None}):
            with patch.object(sys.stdout, "isatty", return_value=False):
                with pytest.raises(SystemExit):
                    _load_yaml(path)
    finally:
        os.unlink(path)


def test_policy_apply_prints_422_error_list(tmp_path, capsys):
    import json as _json
    from modus.__main__ import policy_apply

    f = tmp_path / "p.json"
    f.write_text(_json.dumps({"version": "1", "policies": [
        {"name": "t", "type": "budget_cap", "team": "dev",
         "config": {"cap_usd": "100", "period": "daily"}},
    ]}))
    detail = {"message": "Policy file rejected; nothing was applied.",
              "errors": ["policies[0] (t): team 'dev' not found"]}
    with patch("modus.__main__._post", MagicMock(return_value=(422, {"detail": detail}))):
        assert policy_apply(str(f), url="http://test", token="tok") == 1
    out = capsys.readouterr().out
    assert "team 'dev' not found" in out


def test_policy_diff_reads_policy_type_from_api_list_shape():
    from modus.__main__ import _policy_diff
    desired = {"name": "x", "type": "budget_cap", "scope": "team", "effect": "deny"}
    current = {"name": "x", "policy_type": "budget_cap", "scope": "team", "effect": "deny",
               "priority": 100, "is_active": True}
    assert _policy_diff({**desired, "priority": 100}, current, {}) == []
