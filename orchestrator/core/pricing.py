"""
Modus — Token Cost Estimation
======================================
Cheap, fast token → cost estimation for the /evaluate pre-request gateway.
Uses the same bundled pricing data as pricing_sync.py.

Pure in-memory lookup by default (~0.01 ms per call).
When an optional db session is provided, checks the pricing_overrides table
first — enterprise negotiated rates take precedence over bundled pricing.
Falls back to conservative defaults for unknown models.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

# Bundled pricing: (provider, model) -> (input_cost_per_1k, output_cost_per_1k)
# Aligned with SDK pricing.py (authoritative source).
_PRICING: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    # Anthropic — aligned with SDK pricing.py (rates verified 2026-08-24)
    # NOTE: prefix matching below walks this dict in insertion order and takes
    # the FIRST match, so longer/more-specific keys must be listed first.
    ("anthropic", "claude-fable-5"): (Decimal("0.010000"), Decimal("0.050000")),
    ("anthropic", "claude-mythos-5"): (Decimal("0.010000"), Decimal("0.050000")),
    ("anthropic", "claude-opus-4-8"): (Decimal("0.005000"), Decimal("0.025000")),
    ("anthropic", "claude-opus-4-7"): (Decimal("0.005000"), Decimal("0.025000")),
    ("anthropic", "claude-opus-4-6"): (Decimal("0.005000"), Decimal("0.025000")),
    ("anthropic", "claude-opus-4-5"): (Decimal("0.015000"), Decimal("0.075000")),
    ("anthropic", "claude-opus-5"): (Decimal("0.005000"), Decimal("0.025000")),
    ("anthropic", "claude-sonnet-4-6"): (Decimal("0.003000"), Decimal("0.015000")),
    ("anthropic", "claude-sonnet-4-5"): (Decimal("0.003000"), Decimal("0.015000")),
    ("anthropic", "claude-sonnet-5"): (Decimal("0.003000"), Decimal("0.015000")),
    ("anthropic", "claude-haiku-4-5"): (Decimal("0.001000"), Decimal("0.005000")),
    ("anthropic", "claude-haiku-3"): (Decimal("0.000250"), Decimal("0.001250")),
    # OpenAI — aligned with SDK pricing.py
    ("openai", "gpt-4o"): (Decimal("0.002500"), Decimal("0.010000")),
    ("openai", "gpt-4o-mini"): (Decimal("0.000150"), Decimal("0.000600")),
    ("openai", "gpt-4-turbo"): (Decimal("0.010000"), Decimal("0.030000")),
    ("openai", "gpt-4-5"): (Decimal("0.075000"), Decimal("0.150000")),
    ("openai", "gpt-4.1"): (Decimal("0.002000"), Decimal("0.008000")),
    ("openai", "gpt-4.1-mini"): (Decimal("0.000400"), Decimal("0.001600")),
    ("openai", "gpt-4.1-nano"): (Decimal("0.000100"), Decimal("0.000400")),
    ("openai", "o1"): (Decimal("0.015000"), Decimal("0.060000")),
    ("openai", "o1-mini"): (Decimal("0.001100"), Decimal("0.004400")),
    ("openai", "o3"): (Decimal("0.010000"), Decimal("0.040000")),
    ("openai", "o3-mini"): (Decimal("0.001100"), Decimal("0.004400")),
    ("openai", "o4-mini"): (Decimal("0.001100"), Decimal("0.004400")),
    # Bedrock — aligned with SDK pricing.py
    ("bedrock", "anthropic.claude-3-5-sonnet"): (Decimal("0.003000"), Decimal("0.015000")),
    ("bedrock", "amazon.titan-text-express"): (Decimal("0.000800"), Decimal("0.001600")),
    ("bedrock", "meta.llama3-70b"): (Decimal("0.000990"), Decimal("0.000990")),
    # Azure — aligned with SDK pricing.py
    ("azure", "gpt-4o"): (Decimal("0.002500"), Decimal("0.010000")),
    ("azure", "gpt-4o-mini"): (Decimal("0.000150"), Decimal("0.000600")),
    # GCP / Google — aligned with SDK pricing.py
    ("gcp", "gemini-1.5-pro"): (Decimal("0.001250"), Decimal("0.005000")),
    ("gcp", "gemini-1.5-flash"): (Decimal("0.000075"), Decimal("0.000300")),
    ("gcp", "gemini-2.0-flash"): (Decimal("0.000100"), Decimal("0.000400")),
    # Databricks
    ("databricks", "dbrx-instruct"): (Decimal("0.000750"), Decimal("0.002250")),
    # Mistral — aligned with SDK pricing.py
    ("mistral", "mistral-large"): (Decimal("0.002000"), Decimal("0.006000")),
    ("mistral", "mixtral-8x7b"): (Decimal("0.000700"), Decimal("0.000700")),
    ("mistral", "mistral-7b"): (Decimal("0.000250"), Decimal("0.000250")),
    # Groq — aligned with SDK pricing.py
    ("groq", "llama-3.3-70b"): (Decimal("0.000590"), Decimal("0.000790")),
    ("groq", "llama-3.1-8b"): (Decimal("0.000050"), Decimal("0.000080")),
    ("groq", "gemma-7b"): (Decimal("0.000070"), Decimal("0.000070")),
}

# Conservative fallback for unknown models — priced like mid-range model
_FALLBACK_INPUT = Decimal("0.003000")
_FALLBACK_OUTPUT = Decimal("0.015000")


async def _lookup_override(
    db,
    provider: str,
    model: str,
) -> Optional[tuple[Decimal, Decimal]]:
    """Query pricing_overrides for an active override matching provider+model.

    Returns (input_cost_per_1k, output_cost_per_1k) or None.
    """
    from sqlalchemy import select
    from orchestrator.db.models import PricingOverride

    q = (
        select(PricingOverride)
        .where(
            PricingOverride.provider == provider,
            PricingOverride.model == model,
            PricingOverride.is_active == True,  # noqa: E712
        )
        .limit(1)
    )
    result = await db.execute(q)
    row = result.scalars().first()
    if row is None:
        return None

    input_rate = row.input_cost_per_1k
    output_rate = row.output_cost_per_1k
    if input_rate is None and output_rate is None:
        return None

    return (
        input_rate if input_rate is not None else _FALLBACK_INPUT,
        output_rate if output_rate is not None else _FALLBACK_OUTPUT,
    )


def estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int = 0,
    override_rates: Optional[tuple[Decimal, Decimal]] = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Estimate cost from token counts.

    When *override_rates* is supplied (a ``(input_cost_per_1k,
    output_cost_per_1k)`` tuple), those rates take precedence over the
    bundled ``_PRICING`` dict.  Callers that have an async DB session can
    pre-fetch the override via :func:`_lookup_override` and pass the result
    here — this keeps the hot-path function synchronous.

    Returns:
        (input_cost, output_cost, total_cost) as Decimal values.

    Uses prefix matching for model names (e.g. "claude-sonnet-4-5-20251022"
    matches "claude-sonnet-4-5"). Falls back to conservative defaults for
    unknown models.
    """
    if override_rates is not None:
        pricing = override_rates
    else:
        provider_lower = provider.lower()
        model_lower = model.lower()

        # Exact match first
        pricing = _PRICING.get((provider_lower, model_lower))

        # Prefix match if no exact match
        if pricing is None:
            for (p, m), rates in _PRICING.items():
                if p == provider_lower and model_lower.startswith(m):
                    pricing = rates
                    break

        if pricing is None:
            pricing = (_FALLBACK_INPUT, _FALLBACK_OUTPUT)

    input_rate, output_rate = pricing

    input_cost = Decimal(str(input_tokens)) * input_rate / Decimal("1000")
    output_cost = Decimal(str(output_tokens)) * output_rate / Decimal("1000")
    total_cost = input_cost + output_cost

    return input_cost, output_cost, total_cost
