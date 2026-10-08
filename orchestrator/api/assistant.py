"""
Modus -- Global LLM Assistant API
POST /api/v1/assistant/chat -- send a message, get a response from the
customer's own configured LLM provider.

Uses stdlib ``urllib.request`` only (no httpx, no requests).  The LLM
call runs inside the customer's infrastructure -- Law #4 is satisfied
because the API key and provider belong to the customer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestrator.core.auth import Identity, get_identity

from orchestrator.core.config import get_settings

logger = logging.getLogger(__name__)

assistant_router = APIRouter()

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    context_view: Optional[str] = None
    context_data: Optional[dict] = None


class ChatResponse(BaseModel):
    response: str


# ---------------------------------------------------------------------------
# System prompts per dashboard view
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS: dict[str, str] = {
    "governance": (
        "You are a governance compliance analyst for an AI cost governance "
        "platform called Modus. Help the user understand policy compliance, "
        "enforcement actions, constitutional rules, and governance best practices. "
        "Be precise and cite specific metrics when possible."
    ),
    "finance": (
        "You are a financial analyst reviewing AI infrastructure costs on "
        "Modus. Help the user understand spending trends, cost breakdowns, "
        "budget forecasts, anomalies, and savings opportunities. "
        "Use numbers and percentages when available."
    ),
    "devops": (
        "You are a DevOps engineer analyzing AI agent performance on Modus. "
        "Help the user understand latency, error rates, throughput, deployment "
        "health, and infrastructure metrics. Be technical and actionable."
    ),
    "executive": (
        "You are an executive advisor summarizing AI cost governance data from "
        "Modus. Provide high-level insights, strategic recommendations, and "
        "business impact analysis. Keep it concise and decision-oriented."
    ),
    "policies": (
        "You are a policy specialist for Modus. Help the user understand "
        "policy configuration, enforcement rules, spending limits, and how "
        "to set up effective AI governance policies."
    ),
    "routing": (
        "You are a routing optimization expert for Modus. Help the user "
        "understand model routing decisions, cost-quality tradeoffs, and how "
        "to configure routing rules for optimal efficiency."
    ),
}

_DEFAULT_SYSTEM = (
    "You are an AI assistant for Modus, an AI cost governance platform. "
    "Help the user understand their AI usage data, costs, policies, and "
    "governance posture. Be helpful, concise, and data-driven."
)


def _build_system_prompt(view: str | None, context_data: dict | None) -> str:
    prompt = _SYSTEM_PROMPTS.get(view or "", _DEFAULT_SYSTEM)
    if context_data:
        prompt += (
            "\n\nThe user is currently viewing the following context data:\n"
            + json.dumps(context_data, indent=2, default=str)
        )
    return prompt


# ---------------------------------------------------------------------------
# Provider call helpers (stdlib only)
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()


def _call_anthropic(api_key: str, model: str, system: str, message: str) -> str:
    """Call Anthropic Messages API and return the text response."""
    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": model or "claude-sonnet-5",
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": message}],
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=60) as resp:
        body = json.loads(resp.read())
    # Extract text from content blocks
    blocks = body.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _call_openai(api_key: str, model: str, system: str, message: str) -> str:
    """Call OpenAI Chat Completions API and return the text response."""
    url = "https://api.openai.com/v1/chat/completions"
    payload = json.dumps({
        "model": model or "gpt-4o",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=60) as resp:
        body = json.loads(resp.read())
    choices = body.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _call_google(api_key: str, model: str, system: str, message: str) -> str:
    """Call Google Gemini API and return the text response."""
    model = model or "gemini-2.0-flash"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent?key={api_key}"
    )
    payload = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": message}]}],
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=60) as resp:
        body = json.loads(resp.read())
    candidates = body.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    return ""


def _call_ollama(api_key: str, model: str, system: str, message: str) -> str:
    """Call a local Ollama server — air-gap-safe, no egress to a third party.

    Dashboard context stays on the customer's own infrastructure. api_key is
    unused (local server); the endpoint comes from settings.assistant_ollama_url.
    """
    base = get_settings().assistant_ollama_url.rstrip("/")
    url = f"{base}/api/chat"
    payload = json.dumps({
        "model": model or "llama3.1",
        "system": system,
        "messages": [{"role": "user", "content": message}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    # Local endpoint — no TLS context needed (and often plain HTTP on loopback).
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    return body.get("message", {}).get("content", "")


_PROVIDERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "google": _call_google,
    "ollama": _call_ollama,
}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@assistant_router.post("/assistant/chat", response_model=ChatResponse)
async def assistant_chat(req: ChatRequest, identity: Identity = Depends(get_identity)):
    """Send a question to the customer's configured LLM and return the answer."""
    cfg = get_settings()

    if not cfg.assistant_enabled:
        raise HTTPException(
            status_code=503,
            detail="AI Assistant is not enabled. Configure it in Settings > LLM Configuration.",
        )

    provider = cfg.assistant_provider.lower().strip()
    api_key = cfg.assistant_api_key.strip()
    model = cfg.assistant_model.strip()

    # Ollama is a local server and needs no API key; every other provider does.
    if not provider or (provider != "ollama" and not api_key):
        raise HTTPException(
            status_code=503,
            detail="AI Assistant provider or API key not configured.",
        )

    caller = _PROVIDERS.get(provider)
    if not caller:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported assistant provider: {provider}. "
                   f"Supported: {', '.join(sorted(_PROVIDERS))}.",
        )

    system = _build_system_prompt(req.context_view, req.context_data)

    try:
        # Provider calls use blocking urllib with up to a 60s timeout — run in
        # a worker thread so the event loop keeps serving every other request.
        text = await asyncio.to_thread(caller, api_key, model, system, req.message)
    except urllib.error.HTTPError as exc:
        logger.warning("Assistant LLM HTTP error: %s %s", exc.code, exc.reason)
        raise HTTPException(
            status_code=502,
            detail=f"LLM provider returned HTTP {exc.code}: {exc.reason}",
        )
    except Exception as exc:
        logger.exception("Assistant LLM call failed")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach LLM provider: {exc}",
        )

    return ChatResponse(response=text)
