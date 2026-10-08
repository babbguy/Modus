"""
Modus — AI Provider Factory
================================
Registry-based factory. Each provider self-registers via the @register decorator.
No if/else chains — adding a new provider is a single new file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.core.ai_engine.base import BaseAIProvider

_REGISTRY: dict[str, type[BaseAIProvider]] = {}


def register(name: str):
    """Class decorator that registers an AI provider under *name*.

    Usage::

        @register("openai")
        class OpenAIProvider(BaseAIProvider):
            ...
    """
    def wrapper(cls):
        if name in _REGISTRY:
            import logging
            logging.getLogger(__name__).warning(
                "AI provider %r already registered (%s), overwriting with %s",
                name, _REGISTRY[name].__name__, cls.__name__,
            )
        _REGISTRY[name] = cls
        return cls
    return wrapper


def get_provider(agent: str, api_key: str, model: str, base_url: str) -> BaseAIProvider:
    """Instantiate the provider matching *agent*.

    Raises ``ValueError`` with a list of valid providers on mismatch.
    """
    cls = _REGISTRY.get(agent)
    if cls is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none loaded)"
        raise ValueError(
            f"Unknown summary_agent '{agent}'. "
            f"Available providers: {available}. "
            f"Set MODUS_SUMMARY_AGENT to one of these values."
        )
    return cls(api_key=api_key, model=model, base_url=base_url)


def available_providers() -> list[str]:
    """Return sorted list of registered provider names."""
    return sorted(_REGISTRY.keys())
