"""Modus — Ollama AI Provider (local models)."""

from __future__ import annotations

from orchestrator.core.ai_engine.base import BaseAIProvider
from orchestrator.core.ai_engine.factory import register

DEFAULT_MODEL = "llama3.2"
DEFAULT_BASE_URL = "http://localhost:11434"


@register("ollama")
class OllamaProvider(BaseAIProvider):

    async def summarize(self, prompt: str) -> str:
        model = self.model or DEFAULT_MODEL
        base = self.base_url or DEFAULT_BASE_URL

        from orchestrator.core.tls import get_httpx_client

        is_local = base.startswith("http://localhost") or base.startswith("http://127.")
        async with get_httpx_client(timeout=60, enforce_tls=not is_local) as client:
            resp = await client.post(
                f"{base}/api/chat",
                headers={"Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            try:
                return data["message"]["content"].strip()
            except (KeyError, TypeError) as exc:
                raise ValueError(f"Unexpected Ollama response shape: {exc}") from exc
