"""Modus — OpenAI AI Provider (GPT)."""

from __future__ import annotations

from orchestrator.core.ai_engine.base import BaseAIProvider
from orchestrator.core.ai_engine.factory import register

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com"


@register("openai")
class OpenAIProvider(BaseAIProvider):

    async def summarize(self, prompt: str) -> str:
        model = self.model or DEFAULT_MODEL
        base = self.base_url or DEFAULT_BASE_URL

        from orchestrator.core.tls import get_httpx_client

        async with get_httpx_client(timeout=30) as client:
            resp = await client.post(
                f"{base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
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
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(f"Unexpected OpenAI response shape: {exc}") from exc
