"""Modus — Google AI Provider (Gemini)."""

from __future__ import annotations

from orchestrator.core.ai_engine.base import BaseAIProvider
from orchestrator.core.ai_engine.factory import register

DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


@register("google")
class GoogleProvider(BaseAIProvider):

    async def summarize(self, prompt: str) -> str:
        model = self.model or DEFAULT_MODEL
        base = self.base_url or DEFAULT_BASE_URL

        from orchestrator.core.tls import get_httpx_client

        async with get_httpx_client(timeout=30) as client:
            resp = await client.post(
                f"{base}/v1beta/models/{model}:generateContent",
                params={"key": self.api_key},
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [
                        {"parts": [{"text": prompt}]}
                    ],
                    "generationConfig": {
                        "maxOutputTokens": 400,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(f"Unexpected Google response shape: {exc}") from exc
