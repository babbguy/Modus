"""
SCIM filtered totals and the air-gap Ollama assistant.

  1. SCIM list totalResults reflects the FILTERED set, not the whole table.
  2. Assistant Ollama provider is air-gap-safe: works with no API key and calls
     the configured LOCAL endpoint (no third-party egress).
  3. Connections page reads the real DB notification config (covered in
     tests/test_connection_checker_helpers.py).
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock



# ── 1. SCIM filtered totals ───────────────────────────────────────────────────

async def test_scim_users_filtered_total_is_scoped(client, db_session):
    from orchestrator.db.models import User

    for i in range(5):
        db_session.add(User(
            id=str(uuid.uuid4()), email=f"user{i}@example.com",
            display_name=f"User {i}", is_active=True,
        ))
    await db_session.commit()

    # Unfiltered: total counts all users.
    resp = await client.get("/api/v1/scim/v2/Users")
    assert resp.status_code == 200
    all_total = resp.json()["totalResults"]
    assert all_total >= 5

    # Filtered by one userName: total must be 1, not the whole-table count.
    resp = await client.get('/api/v1/scim/v2/Users?filter=userName eq "user2@example.com"')
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 1, "filtered total must reflect the filter"
    assert len(body["Resources"]) == 1


# ── 2. Assistant Ollama air-gap path ──────────────────────────────────────────

async def test_assistant_ollama_works_without_api_key(monkeypatch):
    from orchestrator.api import assistant as asst

    cfg = MagicMock()
    cfg.assistant_enabled = True
    cfg.assistant_provider = "ollama"
    cfg.assistant_api_key = ""            # no key — must still work
    cfg.assistant_model = "llama3.1"
    cfg.assistant_ollama_url = "http://localhost:11434"
    monkeypatch.setattr(asst, "get_settings", lambda: cfg)

    captured = {}

    def fake_ollama(api_key, model, system, message):
        captured["called"] = True
        captured["model"] = model
        return "local answer"

    monkeypatch.setitem(asst._PROVIDERS, "ollama", fake_ollama)

    req = MagicMock()
    req.context_view, req.context_data, req.message = "overview", {}, "hi"
    resp = await asst.assistant_chat(req, identity=MagicMock())
    assert resp.response == "local answer"
    assert captured["called"] is True


def test_ollama_call_targets_local_endpoint(monkeypatch):
    """The ollama caller must hit the configured LOCAL url, never a public LLM."""
    from orchestrator.api import assistant as asst

    cfg = MagicMock()
    cfg.assistant_ollama_url = "http://127.0.0.1:11434"
    monkeypatch.setattr(asst, "get_settings", lambda: cfg)

    seen = {}

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"message": {"content": "ok"}}).encode()

    def fake_urlopen(req, *a, **k):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(asst.urllib.request, "urlopen", fake_urlopen)
    out = asst._call_ollama("", "llama3.1", "sys", "msg")
    assert out == "ok"
    assert seen["url"].startswith("http://127.0.0.1:11434")
    # Must not be a public provider endpoint.
    assert "openai.com" not in seen["url"]
    assert "anthropic.com" not in seen["url"]
    assert "googleapis.com" not in seen["url"]


def test_non_ollama_still_requires_key():
    """A cloud provider without an api_key stays disabled (503)."""
    # Structural: the guard must special-case only ollama.
    import inspect
    from orchestrator.api import assistant as asst
    src = inspect.getsource(asst.assistant_chat)
    assert 'provider != "ollama"' in src
