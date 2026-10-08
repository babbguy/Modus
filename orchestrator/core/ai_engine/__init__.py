"""
Modus — AI Summary Engine
==============================
Universal facade for AI-powered topology summarization.

Usage::

    from orchestrator.core.ai_engine import AIEngine

    summary = await AIEngine.summarize(snapshot)

The active provider is selected via ``MODUS_SUMMARY_AGENT``.
On any failure (missing key, invalid provider, network error) the engine
falls back to a structured text summary and logs a helpful message.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Import providers so their @register decorators execute on first import.
# Each module is self-contained — adding a new provider only requires
# creating a new file with @register("name").
from orchestrator.core.ai_engine import (  # noqa: F401
    anthropic,
    deepseek,
    google,
    ollama,
    openai,
)
from orchestrator.core.ai_engine.factory import available_providers, get_provider


class AIEngine:
    """Provider-agnostic facade for topology summarization."""

    @staticmethod
    async def summarize(snapshot: dict) -> str:
        """Generate an AI summary of *snapshot*, falling back gracefully."""
        from orchestrator.core.config import settings

        agent = settings.summary_agent
        api_key = settings.summary_api_key.get_secret_value()
        model = settings.summary_model_id
        base_url = settings.summary_base_url

        # Providers that don't need an API key (e.g. Ollama running locally)
        _KEYLESS_PROVIDERS = {"ollama"}

        # Default (empty agent): use local text fallback, no external calls
        if not agent:
            logger.debug(
                "MODUS_SUMMARY_AGENT not configured. Using local text summary. "
                "Set MODUS_SUMMARY_AGENT=ollama for local AI, or anthropic/openai/google/deepseek "
                "to send topology data to external providers (requires API key)."
            )
            return _fallback_summary(snapshot)

        if not api_key and agent not in _KEYLESS_PROVIDERS:
            logger.info(
                "No MODUS_SUMMARY_API_KEY set for provider '%s'. Using structured text fallback. "
                "To enable AI summaries, set MODUS_SUMMARY_AGENT=%s and "
                "MODUS_SUMMARY_API_KEY=<your-key>.",
                agent,
                agent,
            )
            return _fallback_summary(snapshot)

        try:
            provider = get_provider(agent, api_key, model, base_url)
            prompt = _build_summary_prompt(snapshot)
            return await provider.summarize(prompt)
        except ValueError as exc:
            # Invalid provider name
            logger.warning(
                "AI engine configuration error: %s. "
                "Available providers: %s. Falling back to structured text.",
                exc,
                ", ".join(available_providers()),
            )
        except Exception as exc:
            logger.warning("AI summary generation failed (%s): %s", agent, exc)

        return _fallback_summary(snapshot)


# ── Prompt & fallback (provider-agnostic) ────────────────────────────────────


def _build_summary_prompt(snap: dict) -> str:
    """Build a standardised prompt from the topology snapshot.

    This is identical regardless of which AI provider is active, ensuring
    consistent quality across brands.
    """
    routes_desc = ""
    routes = snap.get("api_routes", [])
    if routes:
        ai_routes = [r for r in routes if r.get("likely_ai_endpoint")]
        routes_desc = (
            f"{len(routes)} API routes discovered"
            + (f", {len(ai_routes)} likely AI-related" if ai_routes else "")
            + f". Sample paths: {', '.join(r['path'] for r in routes[:5])}"
        )

    deps = snap.get("service_dependencies", {})
    ai_providers = snap.get("ai_providers", {})
    ai_frameworks = snap.get("ai_frameworks", {})
    infra = snap.get("infrastructure", {})

    return (
        "You are analyzing a microservice that has registered with an AI cost "
        "governance platform.\n"
        "Write a 2-3 sentence technical summary of what this service appears to "
        "do and how it uses AI.\n"
        "Be specific. Use the data provided. Do not use filler phrases.\n\n"
        "Service data:\n"
        f"- Name: {snap.get('app_name', 'unknown')}\n"
        f"- Environment: {snap.get('environment', 'unknown')}\n"
        f"- Runtime: Python {snap.get('python_version', '?')} on "
        f"{snap.get('platform_name', '?')}\n"
        f"- Deployment: {snap.get('deployment_type', 'unknown')} / "
        f"{snap.get('cloud_provider', 'unknown')}\n"
        f"- Web framework: {snap.get('web_framework', 'none detected')}\n"
        f"- AI providers installed: "
        f"{', '.join(f'{k} {v}' for k, v in ai_providers.items()) or 'none'}\n"
        f"- AI frameworks installed: "
        f"{', '.join(f'{k} {v}' for k, v in ai_frameworks.items()) or 'none'}\n"
        f"- Infrastructure: {', '.join(infra.keys()) or 'none detected'}\n"
        f"- Service dependencies: {', '.join(deps.keys()) or 'none detected'}\n"
        f"- {routes_desc or 'No routes extracted'}\n\n"
        "Reply with only the summary. No headers, no bullet points, no preamble."
    )


def _fallback_summary(snap: dict) -> str:
    """Structured text summary when no AI provider is available."""
    parts = []
    name = snap.get("app_name", snap.get("app_id", "Unknown"))
    fw = snap.get("web_framework", "")
    providers = list(snap.get("ai_providers", {}).keys())
    frameworks = list(snap.get("ai_frameworks", {}).keys())
    deployment = snap.get("deployment_type", "unknown")
    deps = list(snap.get("service_dependencies", {}).keys())

    parts.append(f"{name} is a Python service")
    if fw:
        parts[-1] += f" built on {fw}"
    parts[-1] += f" running in {deployment}."

    if providers:
        parts.append(f"Uses AI providers: {', '.join(providers)}.")
    if frameworks:
        parts.append(f"AI frameworks: {', '.join(frameworks)}.")
    if deps:
        parts.append(f"Connected services: {', '.join(deps)}.")

    routes = snap.get("api_routes", [])
    if routes:
        ai_routes = [r for r in routes if r.get("likely_ai_endpoint")]
        parts.append(
            f"Exposes {len(routes)} API endpoints"
            + (f", {len(ai_routes)} likely AI-facing" if ai_routes else "")
            + "."
        )

    return " ".join(parts)
