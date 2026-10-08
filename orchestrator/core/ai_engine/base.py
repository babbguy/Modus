"""
Modus — Base AI Provider
============================
Abstract interface that all AI summarization providers must implement.
Adding a new provider requires only subclassing this and decorating with @register.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAIProvider(ABC):
    """Strategy interface for AI topology summarization."""

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def __repr__(self) -> str:
        key_hint = f"{self.api_key[:4]}…" if self.api_key else "(none)"
        return (
            f"{self.__class__.__name__}(api_key={key_hint!r}, "
            f"model={self.model!r}, base_url={self.base_url!r})"
        )

    @abstractmethod
    async def summarize(self, prompt: str) -> str:
        """Send the prompt to the provider and return the text response.

        Must raise on non-2xx responses so the caller can fall back.
        """
        ...
