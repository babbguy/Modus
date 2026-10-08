"""
modus.pricing
======================
Bundled pricing table for all supported AI providers.

Prices are in USD per 1,000 tokens (input / output).
Source: provider pricing pages. Anthropic rates verified 2026-08-24;
other providers as of March 2026.

The SDK uses these for:
  1. Pre-call cost estimation in enforce() — so budget_cap policies fire correctly
  2. Post-call cost recording — sent to the orchestrator for dashboard display

The orchestrator's pricing_models table is the authoritative source.
These defaults ensure the agent works correctly even when the pricing API
is unreachable, and cost estimates are close enough for enforcement decisions.

To update: edit PRICING_TABLE below. Format:
    "provider:model_prefix": (input_per_1k_usd, output_per_1k_usd)

Model matching uses prefix matching — "claude-haiku" matches all haiku variants.
The most specific (longest) match wins.
"""

from __future__ import annotations
from decimal import Decimal
from typing import Optional, Tuple

# ── Pricing table ─────────────────────────────────────────────────────────────
# Format: "provider:model_prefix" → (input_usd_per_1k, output_usd_per_1k)
# Listed most-specific to least-specific within each provider.

_RAW: list[tuple[str, float, float]] = [
    # ── Anthropic ─────────────────────────────────────────────────────────────
    # Current-generation first-party API rates. Bedrock and Vertex are
    # partner-operated and priced separately — see the "bedrock:" rows.
    # Fable 5 / Mythos 5 — $10 / $50 per 1M
    ("anthropic:claude-fable-5",               0.010000,  0.050000),
    ("anthropic:claude-fable",                 0.010000,  0.050000),
    ("anthropic:claude-mythos-5",              0.010000,  0.050000),
    ("anthropic:claude-mythos",                0.010000,  0.050000),
    # Opus 5 / 4.8 / 4.7 / 4.6 — $5 / $25 per 1M
    ("anthropic:claude-opus-5",                0.005000,  0.025000),
    ("anthropic:claude-opus-4-8",              0.005000,  0.025000),
    ("anthropic:claude-opus-4-7",              0.005000,  0.025000),
    ("anthropic:claude-opus-4-6",              0.005000,  0.025000),
    # Opus 4.5 and earlier — legacy $15 / $75 per 1M
    ("anthropic:claude-opus-4-5",              0.015000,  0.075000),
    ("anthropic:claude-opus-4",                0.015000,  0.075000),
    ("anthropic:claude-opus-3",                0.015000,  0.075000),
    # Opus family catch-all stays at the legacy (higher) rate: an unrecognised
    # opus model must never be under-priced, or a budget_cap under-counts.
    ("anthropic:claude-opus",                  0.015000,  0.075000),
    # Sonnet 5 / 4.6 and earlier — $3 / $15 per 1M
    # (Sonnet 5 carries a $2 / $10 introductory rate through 2026-08-31; the
    #  standard rate is bundled so enforcement never under-counts.)
    ("anthropic:claude-sonnet-5",              0.003000,  0.015000),
    ("anthropic:claude-sonnet-4-6",            0.003000,  0.015000),
    ("anthropic:claude-sonnet-4-5",            0.003000,  0.015000),
    ("anthropic:claude-sonnet-4",              0.003000,  0.015000),
    ("anthropic:claude-sonnet-3-7",            0.003000,  0.015000),
    ("anthropic:claude-sonnet-3-5",            0.003000,  0.015000),
    ("anthropic:claude-sonnet",                0.003000,  0.015000),
    # Haiku 4.5 — $1 / $5 per 1M; Haiku 4 / 3.5 — $0.80 / $4 per 1M
    ("anthropic:claude-haiku-4-5",             0.001000,  0.005000),
    ("anthropic:claude-haiku-4",               0.000800,  0.004000),
    ("anthropic:claude-haiku-3-5",             0.000800,  0.004000),
    ("anthropic:claude-haiku-3",               0.000250,  0.001250),
    ("anthropic:claude-haiku",                 0.001000,  0.005000),
    # catch-all claude
    ("anthropic:claude",                       0.003000,  0.015000),

    # ── OpenAI ────────────────────────────────────────────────────────────────
    ("openai:gpt-4.5",                         0.075000,  0.150000),
    ("openai:gpt-4o-mini",                     0.000150,  0.000600),
    ("openai:gpt-4o",                          0.002500,  0.010000),
    ("openai:gpt-4-turbo",                     0.010000,  0.030000),
    ("openai:gpt-4",                           0.030000,  0.060000),
    ("openai:gpt-3.5-turbo",                   0.000500,  0.001500),
    ("openai:o1-mini",                         0.001100,  0.004400),
    ("openai:o1-preview",                      0.015000,  0.060000),
    ("openai:o1",                              0.015000,  0.060000),
    ("openai:o3-mini",                         0.001100,  0.004400),
    ("openai:o3",                              0.010000,  0.040000),
    ("openai:o4-mini",                         0.001100,  0.004400),
    ("openai:gpt-4.1",                         0.002000,  0.008000),
    ("openai:gpt-4.1-mini",                    0.000400,  0.001600),
    ("openai:gpt-4.1-nano",                    0.000100,  0.000400),
    # embeddings (output tokens not applicable — use input only)
    ("openai:text-embedding-3-large",          0.000130,  0.000000),
    ("openai:text-embedding-3-small",          0.000020,  0.000000),
    ("openai:text-embedding-ada-002",          0.000100,  0.000000),
    # catch-all
    ("openai:gpt",                             0.002500,  0.010000),

    # ── xAI (Grok) ────────────────────────────────────────────────────────────
    # xAI uses the OpenAI SDK with a custom base URL
    ("openai:grok-3-mini",                     0.000300,  0.000500),
    ("openai:grok-3",                          0.003000,  0.015000),
    ("openai:grok-2",                          0.002000,  0.010000),
    ("openai:grok-beta",                       0.005000,  0.015000),
    ("xai:grok-3-mini",                        0.000300,  0.000500),
    ("xai:grok-3",                             0.003000,  0.015000),
    ("xai:grok",                               0.002000,  0.010000),

    # ── DeepSeek ─────────────────────────────────────────────────────────────
    ("deepseek:deepseek-chat",                 0.000140,  0.000280),
    ("deepseek:deepseek-coder",                0.000140,  0.000280),
    ("deepseek:deepseek-reasoner",             0.000550,  0.002190),
    ("deepseek:",                              0.000140,  0.000280),

    # ── Google (Gemini) ───────────────────────────────────────────────────────
    ("google:gemini-2.5-pro",                  0.001250,  0.010000),
    ("google:gemini-2.0-flash-thinking",       0.000000,  0.003500),
    ("google:gemini-2.0-flash",                0.000100,  0.000400),
    ("google:gemini-2.0",                      0.000100,  0.000400),
    ("google:gemini-1.5-pro",                  0.001250,  0.005000),
    ("google:gemini-1.5-flash-8b",             0.000037,  0.000150),
    ("google:gemini-1.5-flash",                0.000075,  0.000300),
    ("google:gemini-1.0-pro",                  0.000500,  0.001500),
    ("google:gemini",                          0.000100,  0.000400),
    # Also covers google-generativeai SDK
    ("gemini:gemini",                          0.000100,  0.000400),

    # ── Groq ──────────────────────────────────────────────────────────────────
    ("groq:llama-3.3-70b",                     0.000590,  0.000790),
    ("groq:llama-3.2-90b",                     0.000900,  0.000900),
    ("groq:llama-3.2-11b",                     0.000180,  0.000180),
    ("groq:llama-3.1-70b",                     0.000590,  0.000790),
    ("groq:llama-3.1-8b",                      0.000050,  0.000080),
    ("groq:llama-3-70b",                       0.000590,  0.000790),
    ("groq:llama-3-8b",                        0.000050,  0.000080),
    ("groq:llama",                             0.000590,  0.000790),
    ("groq:mixtral-8x7b",                      0.000240,  0.000240),
    ("groq:gemma-7b",                          0.000070,  0.000070),
    ("groq:gemma2-9b",                         0.000200,  0.000200),
    ("groq:deepseek-r1",                       0.000750,  0.000990),
    ("groq:",                                  0.000590,  0.000790),   # groq catch-all

    # ── Mistral ───────────────────────────────────────────────────────────────
    ("mistral:mistral-large",                  0.002000,  0.006000),
    ("mistral:mistral-medium",                 0.002700,  0.008100),
    ("mistral:mistral-small",                  0.000200,  0.000600),
    ("mistral:mistral-7b",                     0.000250,  0.000250),
    ("mistral:mixtral-8x7b",                   0.000700,  0.000700),
    ("mistral:mixtral-8x22b",                  0.002000,  0.006000),
    ("mistral:codestral",                      0.001000,  0.003000),
    ("mistral:pixtral",                        0.000150,  0.000150),
    ("mistral:",                               0.002000,  0.006000),   # catch-all

    # ── Cohere ────────────────────────────────────────────────────────────────
    ("cohere:command-r-plus",                  0.002500,  0.010000),
    ("cohere:command-r",                       0.000150,  0.000600),
    ("cohere:command-light",                   0.000300,  0.000600),
    ("cohere:command",                         0.001500,  0.002000),
    ("cohere:embed",                           0.000100,  0.000000),
    ("cohere:",                                0.001500,  0.002000),   # catch-all

    # ── AWS Bedrock ───────────────────────────────────────────────────────────
    # Bedrock uses the underlying model's pricing (slightly higher for API overhead)
    ("bedrock:anthropic.claude-3-5-haiku",     0.000800,  0.004000),
    ("bedrock:anthropic.claude-3-5-sonnet",    0.003000,  0.015000),
    ("bedrock:anthropic.claude-3-haiku",       0.000250,  0.001250),
    ("bedrock:anthropic.claude-3-sonnet",      0.003000,  0.015000),
    ("bedrock:anthropic.claude-3-opus",        0.015000,  0.075000),
    ("bedrock:anthropic.claude",               0.003000,  0.015000),
    ("bedrock:amazon.titan-text-lite",         0.000300,  0.000400),
    ("bedrock:amazon.titan-text-express",      0.000800,  0.001600),
    ("bedrock:amazon.titan",                   0.000800,  0.001600),
    ("bedrock:meta.llama3-8b",                 0.000220,  0.000220),
    ("bedrock:meta.llama3-70b",                0.000990,  0.000990),
    ("bedrock:meta.llama3",                    0.000990,  0.000990),
    ("bedrock:cohere.command-r-plus",          0.003000,  0.015000),
    ("bedrock:cohere.command-r",               0.000500,  0.001500),
    ("bedrock:cohere",                         0.001500,  0.002000),
    ("bedrock:mistral.mistral-large",          0.004000,  0.012000),
    ("bedrock:mistral.mixtral",                0.000450,  0.000700),
    ("bedrock:mistral",                        0.002000,  0.006000),
    ("bedrock:ai21.jamba",                     0.002000,  0.010000),
    ("bedrock:",                               0.003000,  0.015000),   # catch-all

    # ── Azure OpenAI ──────────────────────────────────────────────────────────
    # Same models as OpenAI, slightly different endpoint
    ("azure:gpt-4o-mini",                      0.000150,  0.000600),
    ("azure:gpt-4o",                           0.002500,  0.010000),
    ("azure:gpt-4-turbo",                      0.010000,  0.030000),
    ("azure:gpt-4",                            0.030000,  0.060000),
    ("azure:gpt-35-turbo",                     0.000500,  0.001500),
    ("azure:",                                 0.002500,  0.010000),

    # ── Together AI ───────────────────────────────────────────────────────────
    ("together:meta-llama/Meta-Llama-3.1-405B", 0.005000, 0.005000),
    ("together:meta-llama/Meta-Llama-3.1-70B", 0.000880,  0.000880),
    ("together:meta-llama/Meta-Llama-3.1-8B",  0.000200,  0.000200),
    ("together:meta-llama/llama-3",            0.000900,  0.000900),
    ("together:mistralai/Mixtral-8x22B",       0.001200,  0.001200),
    ("together:mistralai/mixtral",             0.000600,  0.000600),
    ("together:Qwen/Qwen2.5-72B",             0.001200,  0.001200),
    ("together:",                              0.000800,  0.000800),

    # ── Perplexity ────────────────────────────────────────────────────────────
    ("perplexity:sonar-pro",                   0.003000,  0.015000),
    ("perplexity:sonar",                       0.001000,  0.001000),
    ("perplexity:llama-3.1-sonar-large",       0.001000,  0.001000),
    ("perplexity:llama-3.1-sonar-small",       0.000200,  0.000200),
    ("perplexity:",                            0.001000,  0.001000),

    # ── Fireworks ─────────────────────────────────────────────────────────────
    ("fireworks:accounts/fireworks/models/llama-v3p1-405b", 0.003000, 0.003000),
    ("fireworks:accounts/fireworks/models/llama-v3p1-70b",  0.000900, 0.000900),
    ("fireworks:accounts/fireworks/models/llama-v3p1-8b",   0.000200, 0.000200),
    ("fireworks:accounts/fireworks/models/mixtral-8x22b",   0.001200, 0.001200),
    ("fireworks:",                                          0.000900, 0.000900),

    # ── NVIDIA ───────────────────────────────────────────────────────────────
    ("nvidia:meta/llama-3.1-405b-instruct",    0.005000,  0.005000),
    ("nvidia:meta/llama-3.1-70b-instruct",     0.000880,  0.000880),
    ("nvidia:",                                0.001000,  0.002000),

    # ── Cerebras ─────────────────────────────────────────────────────────────
    ("cerebras:llama3.1-70b",                  0.000850,  0.000850),
    ("cerebras:llama3.1-8b",                   0.000100,  0.000100),
    ("cerebras:",                              0.000500,  0.001000),

    # ── OpenRouter ───────────────────────────────────────────────────────────
    ("openrouter:anthropic/claude-sonnet-4",   0.003000,  0.015000),
    ("openrouter:openai/gpt-4o",              0.002500,  0.010000),
    ("openrouter:google/gemini-2.0-flash",    0.000100,  0.000400),
    ("openrouter:meta-llama/llama-3.1-405b",  0.003000,  0.003000),
    ("openrouter:",                            0.001000,  0.002000),

    # ── SambaNova ────────────────────────────────────────────────────────────
    ("sambanova:Meta-Llama-3.1-405B",          0.005000,  0.010000),
    ("sambanova:Meta-Llama-3.1-70B",           0.000800,  0.000800),
    ("sambanova:",                             0.001000,  0.002000),

    # ── Vertex AI (Google Cloud) ──────────────────────────────────────────────
    ("vertex:gemini-1.5-pro",                  0.001250,  0.005000),
    ("vertex:gemini-1.5-flash",                0.000075,  0.000300),
    ("vertex:gemini-2",                        0.000100,  0.000400),
    ("vertex:",                                0.000100,  0.000400),

    # ── Generic / unknown ─────────────────────────────────────────────────────
    ("*",                                      0.001000,  0.002000),
]

# Build lookup table sorted by key length descending (most specific first)
_TABLE: list[tuple[str, Decimal, Decimal]] = sorted(
    [
        (key.lower(), Decimal(str(inp)), Decimal(str(out)))
        for key, inp, out in _RAW
    ],
    key=lambda x: len(x[0]),
    reverse=True,
)


def estimate_cost(
    provider: str,
    model: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
) -> Tuple[Optional[Decimal], Optional[Decimal], Decimal]:
    """
    Estimate input_cost, output_cost, and total_cost for a given call.

    Returns: (input_cost, output_cost, total_cost) in USD.
    Returns (None, None, Decimal(0)) if no tokens provided.
    """
    if not input_tokens and not output_tokens:
        return None, None, Decimal("0")

    inp_rate, out_rate = _lookup(provider, model)
    it = input_tokens or 0
    ot = output_tokens or 0

    input_cost  = Decimal(str(it)) / Decimal("1000") * inp_rate if it else None
    output_cost = Decimal(str(ot)) / Decimal("1000") * out_rate if ot else None
    total = (input_cost or Decimal("0")) + (output_cost or Decimal("0"))
    return input_cost, output_cost, total


def estimate_tokens_cost(
    provider: str,
    model: Optional[str],
    max_tokens: Optional[int],
) -> Optional[Decimal]:
    """
    Rough estimated cost for an enforce() pre-call check.
    Uses max_tokens as a worst-case estimate for output tokens.
    Returns None if no information available.
    """
    if not max_tokens:
        return None
    _, out_rate = _lookup(provider, model)
    return Decimal(str(max_tokens)) / Decimal("1000") * out_rate


def _lookup(provider: str, model: Optional[str]) -> Tuple[Decimal, Decimal]:
    """Return (input_per_1k, output_per_1k) for the best matching entry."""
    p = (provider or "").lower()
    m = (model or "").lower()
    candidate = f"{p}:{m}"

    for key, inp, out in _TABLE:
        if key == "*":
            return inp, out
        if candidate.startswith(key) or (not key.endswith(":") and key in candidate):
            return inp, out
        # Also try without model (provider-only match)
        if key.endswith(":") and p == key[:-1]:
            return inp, out

    # Fallback
    return Decimal("0.001"), Decimal("0.002")
