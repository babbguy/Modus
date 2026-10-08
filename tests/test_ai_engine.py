"""
Tests for the multi-provider AI Summary Engine.
Covers: factory, provider registry, fallback, prompt building, AIEngine facade.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pydantic import SecretStr

from orchestrator.core.ai_engine import (
    AIEngine,
    _build_summary_prompt,
    _fallback_summary,
)
from orchestrator.core.ai_engine.base import BaseAIProvider
from orchestrator.core.ai_engine.factory import (
    _REGISTRY,
    available_providers,
    get_provider,
    register,
)


# ── Sample snapshot ──────────────────────────────────────────────────────────

SAMPLE_SNAPSHOT = {
    "app_name": "recommendation-api",
    "app_id": "rec-api",
    "environment": "production",
    "python_version": "3.12.0",
    "platform_name": "Linux",
    "deployment_type": "kubernetes",
    "cloud_provider": "aws",
    "web_framework": "FastAPI",
    "ai_providers": {"anthropic": "0.34.0", "openai": "1.45.0"},
    "ai_frameworks": {"langchain": "0.2.1"},
    "infrastructure": {"redis": True, "postgresql": True},
    "service_dependencies": {"redis": {"host": "cache"}, "postgresql": {"host": "db"}},
    "api_routes": [
        {"path": "/recommend", "likely_ai_endpoint": True},
        {"path": "/health", "likely_ai_endpoint": False},
        {"path": "/classify", "likely_ai_endpoint": True},
    ],
}


# ── Provider Registry ────────────────────────────────────────────────────────


class TestProviderRegistry:
    def test_all_five_providers_registered(self):
        expected = {"anthropic", "openai", "google", "deepseek", "ollama"}
        assert expected.issubset(set(_REGISTRY.keys()))

    def test_available_providers_sorted(self):
        result = available_providers()
        assert result == sorted(result)
        assert "anthropic" in result
        assert "openai" in result

    def test_register_decorator(self):
        @register("test_provider")
        class TestProvider(BaseAIProvider):
            async def summarize(self, prompt: str) -> str:
                return "test"

        assert "test_provider" in _REGISTRY
        assert _REGISTRY["test_provider"] is TestProvider
        # Cleanup
        del _REGISTRY["test_provider"]

    def test_get_provider_valid(self):
        provider = get_provider("anthropic", "key", "model", "")
        assert isinstance(provider, BaseAIProvider)
        assert provider.api_key == "key"
        assert provider.model == "model"

    def test_get_provider_invalid(self):
        with pytest.raises(ValueError, match="Unknown summary_agent 'nonexistent'"):
            get_provider("nonexistent", "", "", "")

    def test_get_provider_error_lists_available(self):
        with pytest.raises(ValueError, match="Available providers:"):
            get_provider("bad_name", "", "", "")


# ── Prompt Building ──────────────────────────────────────────────────────────


class TestPromptBuilding:
    def test_prompt_contains_app_name(self):
        prompt = _build_summary_prompt(SAMPLE_SNAPSHOT)
        assert "recommendation-api" in prompt

    def test_prompt_contains_ai_providers(self):
        prompt = _build_summary_prompt(SAMPLE_SNAPSHOT)
        assert "anthropic" in prompt
        assert "openai" in prompt

    def test_prompt_contains_routes(self):
        prompt = _build_summary_prompt(SAMPLE_SNAPSHOT)
        assert "3 API routes discovered" in prompt
        assert "2 likely AI-related" in prompt
        assert "/recommend" in prompt

    def test_prompt_contains_instructions(self):
        prompt = _build_summary_prompt(SAMPLE_SNAPSHOT)
        assert "2-3 sentence technical summary" in prompt
        assert "No headers, no bullet points" in prompt

    def test_prompt_empty_snapshot(self):
        prompt = _build_summary_prompt({})
        assert "unknown" in prompt
        assert "No routes extracted" in prompt

    def test_prompt_no_ai_providers(self):
        snap = {**SAMPLE_SNAPSHOT, "ai_providers": {}}
        prompt = _build_summary_prompt(snap)
        assert "AI providers installed: none" in prompt


# ── Fallback Summary ─────────────────────────────────────────────────────────


class TestFallbackSummary:
    def test_fallback_basic(self):
        summary = _fallback_summary(SAMPLE_SNAPSHOT)
        assert "recommendation-api" in summary
        assert "FastAPI" in summary
        assert "kubernetes" in summary

    def test_fallback_includes_providers(self):
        summary = _fallback_summary(SAMPLE_SNAPSHOT)
        assert "anthropic" in summary
        assert "openai" in summary

    def test_fallback_includes_frameworks(self):
        summary = _fallback_summary(SAMPLE_SNAPSHOT)
        assert "langchain" in summary

    def test_fallback_includes_dependencies(self):
        summary = _fallback_summary(SAMPLE_SNAPSHOT)
        assert "redis" in summary
        assert "postgresql" in summary

    def test_fallback_includes_routes(self):
        summary = _fallback_summary(SAMPLE_SNAPSHOT)
        assert "3 API endpoints" in summary
        assert "2 likely AI-facing" in summary

    def test_fallback_empty_snapshot(self):
        summary = _fallback_summary({})
        assert "Unknown is a Python service" in summary

    def test_fallback_no_routes(self):
        snap = {**SAMPLE_SNAPSHOT, "api_routes": []}
        summary = _fallback_summary(snap)
        assert "endpoints" not in summary


# ── AIEngine Facade ──────────────────────────────────────────────────────────


class TestAIEngine:
    @pytest.mark.asyncio
    async def test_empty_agent_uses_fallback_immediately(self):
        """Test that empty summary_agent (default) uses local fallback without trying external providers."""
        mock_settings = MagicMock()
        mock_settings.summary_agent = ""  # Default: no external provider
        mock_settings.summary_api_key = SecretStr("")
        mock_settings.summary_model_id = ""
        mock_settings.summary_base_url = ""

        with patch("orchestrator.core.config.settings", mock_settings), \
             patch("orchestrator.core.ai_engine.get_provider") as mock_factory:
            result = await AIEngine.summarize(SAMPLE_SNAPSHOT)

        # Should use fallback immediately, never call get_provider
        mock_factory.assert_not_called()
        assert "recommendation-api" in result
        assert "Python service" in result

    @pytest.mark.asyncio
    async def test_no_api_key_returns_fallback(self):
        mock_settings = MagicMock()
        mock_settings.summary_agent = "anthropic"
        mock_settings.summary_api_key = SecretStr("")
        mock_settings.summary_model_id = ""
        mock_settings.summary_base_url = ""

        with patch("orchestrator.core.config.settings", mock_settings):
            result = await AIEngine.summarize(SAMPLE_SNAPSHOT)

        assert "recommendation-api" in result
        assert "Python service" in result

    @pytest.mark.asyncio
    async def test_ollama_no_key_still_calls_provider(self):
        mock_settings = MagicMock()
        mock_settings.summary_agent = "ollama"
        mock_settings.summary_api_key = SecretStr("")
        mock_settings.summary_model_id = ""
        mock_settings.summary_base_url = ""

        mock_provider = AsyncMock()
        mock_provider.summarize.return_value = "AI-generated summary"

        with patch("orchestrator.core.config.settings", mock_settings), \
             patch("orchestrator.core.ai_engine.get_provider", return_value=mock_provider):
            result = await AIEngine.summarize(SAMPLE_SNAPSHOT)

        assert result == "AI-generated summary"
        mock_provider.summarize.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_fallback(self):
        mock_settings = MagicMock()
        mock_settings.summary_agent = "nonexistent"
        mock_settings.summary_api_key = SecretStr("some-key")
        mock_settings.summary_model_id = ""
        mock_settings.summary_base_url = ""

        with patch("orchestrator.core.config.settings", mock_settings):
            result = await AIEngine.summarize(SAMPLE_SNAPSHOT)

        assert "recommendation-api" in result
        assert "Python service" in result

    @pytest.mark.asyncio
    async def test_provider_error_returns_fallback(self):
        mock_settings = MagicMock()
        mock_settings.summary_agent = "anthropic"
        mock_settings.summary_api_key = SecretStr("test-key")
        mock_settings.summary_model_id = ""
        mock_settings.summary_base_url = ""

        mock_provider = AsyncMock()
        mock_provider.summarize.side_effect = Exception("Connection refused")

        with patch("orchestrator.core.config.settings", mock_settings), \
             patch("orchestrator.core.ai_engine.get_provider", return_value=mock_provider):
            result = await AIEngine.summarize(SAMPLE_SNAPSHOT)

        assert "recommendation-api" in result
        assert "Python service" in result

    @pytest.mark.asyncio
    async def test_successful_provider_call(self):
        mock_settings = MagicMock()
        mock_settings.summary_agent = "openai"
        mock_settings.summary_api_key = SecretStr("sk-test")
        mock_settings.summary_model_id = "gpt-4o"
        mock_settings.summary_base_url = ""

        mock_provider = AsyncMock()
        mock_provider.summarize.return_value = "Generated by GPT-4o"

        with patch("orchestrator.core.config.settings", mock_settings), \
             patch("orchestrator.core.ai_engine.get_provider", return_value=mock_provider) as mock_factory:
            result = await AIEngine.summarize(SAMPLE_SNAPSHOT)

        assert result == "Generated by GPT-4o"
        mock_factory.assert_called_once_with("openai", "sk-test", "gpt-4o", "")

    @pytest.mark.asyncio
    async def test_custom_base_url_passed(self):
        mock_settings = MagicMock()
        mock_settings.summary_agent = "openai"
        mock_settings.summary_api_key = SecretStr("sk-test")
        mock_settings.summary_model_id = ""
        mock_settings.summary_base_url = "https://my-azure.openai.azure.com"

        mock_provider = AsyncMock()
        mock_provider.summarize.return_value = "Azure response"

        with patch("orchestrator.core.config.settings", mock_settings), \
             patch("orchestrator.core.ai_engine.get_provider", return_value=mock_provider) as mock_factory:
            await AIEngine.summarize(SAMPLE_SNAPSHOT)

        mock_factory.assert_called_once_with(
            "openai", "sk-test", "", "https://my-azure.openai.azure.com"
        )


# ── Individual Provider Construction ─────────────────────────────────────────


class TestProviderConstruction:
    def test_anthropic_defaults(self):
        p = get_provider("anthropic", "key", "", "")
        assert p.api_key == "key"
        assert p.model == ""  # empty means use DEFAULT_MODEL in provider

    def test_openai_with_custom_model(self):
        p = get_provider("openai", "key", "gpt-4o", "")
        assert p.model == "gpt-4o"

    def test_google_with_custom_url(self):
        p = get_provider("google", "key", "", "https://custom.googleapis.com")
        assert p.base_url == "https://custom.googleapis.com"

    def test_deepseek_provider(self):
        p = get_provider("deepseek", "key", "", "")
        assert isinstance(p, BaseAIProvider)

    def test_ollama_provider(self):
        p = get_provider("ollama", "", "mistral", "http://gpu-server:11434")
        assert p.model == "mistral"
        assert p.base_url == "http://gpu-server:11434"
