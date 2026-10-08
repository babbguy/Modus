"""
Tests — AI Assistant API
Covers: schema validation, system prompts, provider helpers,
        endpoint error paths.
"""
from __future__ import annotations

# ── Schema tests ─────────────────────────────────────────────────────────────

def test_chat_request_schema():
    from orchestrator.api.assistant import ChatRequest
    req = ChatRequest(message="What is my spend?")
    assert req.message == "What is my spend?"
    assert req.context_view is None
    assert req.context_data is None


def test_chat_request_with_context():
    from orchestrator.api.assistant import ChatRequest
    req = ChatRequest(
        message="Explain this",
        context_view="finance",
        context_data={"total_cost": 1234.56},
    )
    assert req.context_view == "finance"
    assert req.context_data["total_cost"] == 1234.56


def test_chat_response_schema():
    from orchestrator.api.assistant import ChatResponse
    resp = ChatResponse(response="Your spend is $42.")
    assert resp.response == "Your spend is $42."


# ── System prompts ───────────────────────────────────────────────────────────

def test_build_system_prompt_default():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt(None, None)
    assert "Modus" in prompt
    assert "cost governance" in prompt


def test_build_system_prompt_governance():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("governance", None)
    assert "governance" in prompt.lower()


def test_build_system_prompt_finance():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("finance", None)
    assert "financial" in prompt.lower() or "spending" in prompt.lower()


def test_build_system_prompt_devops():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("devops", None)
    assert "DevOps" in prompt


def test_build_system_prompt_executive():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("executive", None)
    assert "executive" in prompt.lower()


def test_build_system_prompt_policies():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("policies", None)
    assert "policy" in prompt.lower()


def test_build_system_prompt_routing():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("routing", None)
    assert "routing" in prompt.lower()


def test_build_system_prompt_with_context_data():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("finance", {"total_cost": 100})
    assert "total_cost" in prompt


def test_build_system_prompt_unknown_view():
    from orchestrator.api.assistant import _build_system_prompt
    prompt = _build_system_prompt("nonexistent_view", None)
    assert "Modus" in prompt  # Falls back to default


# ── Provider registry ────────────────────────────────────────────────────────

def test_providers_registry():
    from orchestrator.api.assistant import _PROVIDERS
    assert "anthropic" in _PROVIDERS
    assert "openai" in _PROVIDERS
    assert "google" in _PROVIDERS
    # Ollama is the air-gap-safe local provider (no egress to a third party).
    assert "ollama" in _PROVIDERS
    assert len(_PROVIDERS) == 4


# ── Endpoint tests ───────────────────────────────────────────────────────────

async def test_assistant_disabled(client):
    from orchestrator.core.config import settings
    original = settings.assistant_enabled
    settings.assistant_enabled = False
    try:
        resp = await client.post("/api/v1/assistant/chat", json={
            "message": "Hello",
        })
        assert resp.status_code == 503
        assert "not enabled" in resp.json()["detail"]
    finally:
        settings.assistant_enabled = original


async def test_assistant_no_provider(client):
    from orchestrator.core.config import settings
    settings.assistant_enabled = True
    original_provider = settings.assistant_provider
    original_key = settings.assistant_api_key
    settings.assistant_provider = ""
    settings.assistant_api_key = ""
    try:
        resp = await client.post("/api/v1/assistant/chat", json={
            "message": "Hello",
        })
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]
    finally:
        settings.assistant_enabled = False
        settings.assistant_provider = original_provider
        settings.assistant_api_key = original_key


async def test_assistant_unsupported_provider(client):
    from orchestrator.core.config import settings
    settings.assistant_enabled = True
    original_provider = settings.assistant_provider
    original_key = settings.assistant_api_key
    original_model = settings.assistant_model
    settings.assistant_provider = "unsupported_llm"
    settings.assistant_api_key = "test-key"
    settings.assistant_model = "test-model"
    try:
        resp = await client.post("/api/v1/assistant/chat", json={
            "message": "Hello",
        })
        assert resp.status_code == 400
        assert "Unsupported" in resp.json()["detail"]
    finally:
        settings.assistant_enabled = False
        settings.assistant_provider = original_provider
        settings.assistant_api_key = original_key
        settings.assistant_model = original_model
