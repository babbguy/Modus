"""Modus — Anthropic AI Provider (Claude)."""

from __future__ import annotations

from orchestrator.core.ai_engine.base import BaseAIProvider
from orchestrator.core.ai_engine.factory import register

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"


@register("anthropic")
class AnthropicProvider(BaseAIProvider):

    async def summarize(self, prompt: str) -> str:
        model = self.model or DEFAULT_MODEL
        base = self.base_url or DEFAULT_BASE_URL

        from orchestrator.core.tls import get_httpx_client

        async with get_httpx_client(timeout=30) as client:
            resp = await client.post(
                f"{base}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            try:
                return data["content"][0]["text"].strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(f"Unexpected Anthropic response shape: {exc}") from exc
