"""
Tests -- AI-written insight text is strictly opt-in.

Privacy law: nothing leaves the customer's infrastructure by default. The
insights engine may call api.anthropic.com only when the operator enabled the
setting AND configured an Anthropic key, and it must send that key.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core import insights_engine as ie
from orchestrator.core.config import get_settings

cfg = get_settings()


@pytest.fixture
def anthropic_key():
    saved = (cfg.assistant_provider, cfg.assistant_api_key)
    object.__setattr__(cfg, "assistant_provider", "anthropic")
    object.__setattr__(cfg, "assistant_api_key", "sk-ant-test-key")
    yield "sk-ant-test-key"
    object.__setattr__(cfg, "assistant_provider", saved[0])
    object.__setattr__(cfg, "assistant_api_key", saved[1])


@pytest.fixture(autouse=True)
def _reset_warning_latch():
    ie._warned_no_key = False
    yield
    ie._warned_no_key = False


def _fake_urlopen_response(text="Short AI sentence."):
    resp = MagicMock()
    resp.read.return_value = json.dumps(
        {"content": [{"type": "text", "text": text}]}
    ).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_all_ai_text_settings_default_off():
    for key in (
        "insights.anomaly.ai_explanations",
        "insights.recommendations.ai_text",
        "insights.efficiency.ai_text",
        "insights.executive.ai_narrative",
    ):
        assert ie._DEFAULTS[key] == "false", key


async def test_defaults_make_no_http_request(db_session):
    """With default settings the three call sites decide 'AI off' and no request is made."""
    with patch.object(ie.urllib_request, "urlopen") as urlopen:
        for key in (
            "insights.anomaly.ai_explanations",
            "insights.recommendations.ai_text",
            "insights.efficiency.ai_text",
        ):
            assert await ie.get_setting_bool(db_session, key, False) is False
    urlopen.assert_not_called()


async def test_enabled_but_no_key_falls_back_without_http(caplog):
    with patch.object(ie.urllib_request, "urlopen") as urlopen, \
            caplog.at_level("WARNING", logger=ie.logger.name):
        out = await ie._ai_explain("sys", "anomaly z-score high")
    urlopen.assert_not_called()
    assert "anomaly" in out.lower()
    assert any("no Anthropic API key" in r.message for r in caplog.records)


async def test_key_for_other_provider_is_not_used():
    saved = (cfg.assistant_provider, cfg.assistant_api_key)
    object.__setattr__(cfg, "assistant_provider", "openai")
    object.__setattr__(cfg, "assistant_api_key", "sk-openai")
    try:
        with patch.object(ie.urllib_request, "urlopen") as urlopen:
            await ie._ai_explain("sys", "user")
        urlopen.assert_not_called()
    finally:
        object.__setattr__(cfg, "assistant_provider", saved[0])
        object.__setattr__(cfg, "assistant_api_key", saved[1])


async def test_opt_in_with_key_sends_auth_header(anthropic_key):
    with patch.object(ie.urllib_request, "urlopen", return_value=_fake_urlopen_response()) as urlopen:
        out = await ie._ai_explain("sys", "user", max_tokens=50)
    assert out == "Short AI sentence."
    req = urlopen.call_args[0][0]
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.get_header("X-api-key") == anthropic_key
    assert req.get_header("Anthropic-version") == "2023-06-01"
    assert urlopen.call_args[1]["timeout"] == 15


async def test_failure_falls_back_and_logs_warning_without_leaking_key(anthropic_key, caplog):
    with patch.object(ie.urllib_request, "urlopen", side_effect=OSError("boom")), \
            caplog.at_level("WARNING", logger=ie.logger.name):
        out = await ie._ai_explain("sys", "anomaly z-score high")
    assert "anomaly" in out.lower()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "failed" in messages and "boom" in messages
    assert anthropic_key not in messages


async def test_efficiency_setting_is_a_valid_settings_key(client):
    r = await client.put(
        "/api/v1/admin/settings/insights.efficiency.ai_text",
        json={"value": "true", "updated_by": "tester"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "true"
    listed = {s["key"]: s["value"] for s in (await client.get("/api/v1/admin/settings")).json()}
    assert listed["insights.efficiency.ai_text"] == "true"


async def test_boolean_setting_rejects_garbage(client):
    r = await client.put(
        "/api/v1/admin/settings/insights.anomaly.ai_explanations",
        json={"value": "maybe", "updated_by": "tester"},
    )
    assert r.status_code == 422


def test_every_ai_call_site_is_gated_by_its_setting():
    """Guard: each `await _ai_explain(` sits directly under an `if ai_enabled` check,
    and `ai_enabled` is always read with a False default."""
    import inspect
    import re

    src = inspect.getsource(ie)
    lines = src.splitlines()
    sites = [i for i, ln in enumerate(lines) if "await _ai_explain(" in ln]
    assert len(sites) == 3
    for i in sites:
        window = "\n".join(lines[max(0, i - 2): i])
        assert "if ai_enabled" in window, f"ungated _ai_explain call near line {i + 1}"
    for m in re.finditer(r"ai_enabled = await get_setting_bool\(\s*db, \"([^\"]+)\", (\w+)\s*\)", src):
        assert m.group(2) == "False", m.group(1)
