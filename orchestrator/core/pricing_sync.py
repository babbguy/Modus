"""
Modus — Pricing Sync
Populates the pricing_models table from bundled pricing data and optionally
fetches live pricing from provider APIs (Phase 4d auto-sync).

Two sync sources:
  1. Bundled: hardcoded in this file, updated at build time. Always runs.
  2. Live fetch: pulls from known public pricing endpoints. Falls back to
     bundled if any fetch fails. Enabled by default.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from urllib import request as urllib_request
from urllib.error import URLError

from sqlalchemy import select

from orchestrator.db.models import PricingModel
from orchestrator.db.session import _session_factory
from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

# Stable base date for the first version of a bundled price. Later corrections
# are versioned with the sync timestamp so the latest-effective row wins.
_BUNDLED_BASE_EFFECTIVE = datetime(2024, 1, 1, tzinfo=timezone.utc)

# Bundled pricing data — aligned with SDK pricing.py (authoritative source)
BUNDLED_PRICING = {
    # Anthropic — rates verified 2026-08-24
    ("anthropic", "claude-fable-5", "llm_call"): (0.010, 0.050),
    ("anthropic", "claude-mythos-5", "llm_call"): (0.010, 0.050),
    ("anthropic", "claude-opus-5", "llm_call"): (0.005, 0.025),
    ("anthropic", "claude-opus-4-8", "llm_call"): (0.005, 0.025),
    ("anthropic", "claude-opus-4-7", "llm_call"): (0.005, 0.025),
    ("anthropic", "claude-opus-4-6", "llm_call"): (0.005, 0.025),
    ("anthropic", "claude-opus-4-5", "llm_call"): (0.015, 0.075),
    ("anthropic", "claude-sonnet-5", "llm_call"): (0.003, 0.015),
    ("anthropic", "claude-sonnet-4-6", "llm_call"): (0.003, 0.015),
    ("anthropic", "claude-sonnet-4-5", "llm_call"): (0.003, 0.015),
    ("anthropic", "claude-haiku-4-5", "llm_call"): (0.001, 0.005),
    ("anthropic", "claude-haiku-3", "llm_call"): (0.00025, 0.00125),
    # OpenAI
    ("openai", "gpt-4o", "llm_call"): (0.0025, 0.01),
    ("openai", "gpt-4o-mini", "llm_call"): (0.00015, 0.0006),
    ("openai", "gpt-4-turbo", "llm_call"): (0.01, 0.03),
    ("openai", "gpt-4.1", "llm_call"): (0.002, 0.008),
    ("openai", "gpt-4.1-mini", "llm_call"): (0.0004, 0.0016),
    ("openai", "o1", "llm_call"): (0.015, 0.06),
    ("openai", "o1-mini", "llm_call"): (0.0011, 0.0044),
    ("openai", "o3", "llm_call"): (0.01, 0.04),
    ("openai", "o3-mini", "llm_call"): (0.0011, 0.0044),
    ("openai", "text-embedding-3-small", "embedding"): (0.00002, 0.0),
    ("openai", "text-embedding-3-large", "embedding"): (0.00013, 0.0),
    # Bedrock
    ("bedrock", "anthropic.claude-3-5-sonnet", "llm_call"): (0.003, 0.015),
    ("bedrock", "amazon.titan-text-express", "llm_call"): (0.0008, 0.0016),
    ("bedrock", "meta.llama3-70b", "llm_call"): (0.00099, 0.00099),
    # Azure
    ("azure", "gpt-4o", "llm_call"): (0.0025, 0.01),
    ("azure", "gpt-4o-mini", "llm_call"): (0.00015, 0.0006),
    # GCP / Google
    ("gcp", "gemini-1.5-pro", "llm_call"): (0.00125, 0.005),
    ("gcp", "gemini-1.5-flash", "llm_call"): (0.000075, 0.0003),
    ("gcp", "gemini-2.0-flash", "llm_call"): (0.0001, 0.0004),
    # Databricks
    ("databricks", "dbrx-instruct", "llm_call"): (0.00075, 0.00225),
    # Groq
    ("groq", "llama-3.3-70b", "llm_call"): (0.00059, 0.00079),
    ("groq", "llama-3.1-8b", "llm_call"): (0.00005, 0.00008),
    # Mistral
    ("mistral", "mistral-large", "llm_call"): (0.002, 0.006),
    ("mistral", "mixtral-8x7b", "llm_call"): (0.0007, 0.0007),
}


def _prices_equal(stored: PricingModel, input_cost: float, output_cost: float) -> bool:
    """Compare a stored price row to a bundled (provider) price."""
    return (
        stored.input_cost_per_1k == Decimal(str(input_cost))
        and stored.output_cost_per_1k == Decimal(str(output_cost))
    )


async def sync_pricing() -> None:
    """Sync bundled pricing into pricing_models with versioned self-correction.

    When a bundled price changes, a NEW row is inserted with a later
    ``effective_from`` (the sync timestamp) so the latest-effective lookup used
    by the pricing/CI APIs returns the corrected price. Unchanged prices are a
    no-op, so re-running sync is idempotent. Previously an unconditional
    ``on_conflict_do_nothing`` on a hardcoded effective_from meant a changed
    bundled price could never propagate — prices silently drifted.
    """
    if _session_factory is None:
        return

    now = datetime.now(timezone.utc)
    inserted = 0
    corrected = 0

    async with _session_factory() as db:
        for (provider, model, resource_type), (input_cost, output_cost) in BUNDLED_PRICING.items():
            # Latest-effective stored price for this (provider, model, resource).
            latest = (await db.execute(
                select(PricingModel)
                .where(
                    PricingModel.provider == provider,
                    PricingModel.model == model,
                    PricingModel.resource_type == resource_type,
                )
                .order_by(PricingModel.effective_from.desc())
                .limit(1)
            )).scalar_one_or_none()

            if latest is None:
                effective_from = _BUNDLED_BASE_EFFECTIVE
                source = "bundled_pricing_v2"
            elif _prices_equal(latest, input_cost, output_cost):
                continue  # unchanged — idempotent no-op
            else:
                # Drift: bundled price differs from the stored latest. Version it.
                logger.warning(
                    "Pricing drift for %s/%s (%s): stored in=%s out=%s -> "
                    "bundled in=%s out=%s; inserting corrected version",
                    provider, model, resource_type,
                    latest.input_cost_per_1k, latest.output_cost_per_1k,
                    input_cost, output_cost,
                )
                effective_from = now
                source = "bundled_pricing_v2_corrected"
                corrected += 1

            db.add(PricingModel(
                provider=provider,
                model=model,
                resource_type=resource_type,
                input_cost_per_1k=Decimal(str(input_cost)),
                output_cost_per_1k=Decimal(str(output_cost)),
                effective_from=effective_from,
                source=source,
            ))
            inserted += 1

        await db.commit()
        logger.info(
            "Pricing sync (bundled) complete",
            extra={"rows_written": inserted, "corrections": corrected},
        )

    # Optional live fetch — gated for air-gap safety (Law 2). Never writes
    # prices; only detects newly-released models the bundle is missing.
    if not settings.pricing_live_fetch_enabled:
        return
    try:
        await _fetch_live_pricing()
    except Exception as exc:
        logger.debug("Live pricing probe skipped: %s", exc)


# ── Live pricing fetch (Phase 4d) ─────────────────────────────────────────────
#
# Fetches pricing from known public endpoints. Each provider has its own
# parser. Failures are non-fatal — bundled pricing is always the fallback.

_PRICING_FETCH_TIMEOUT = 10  # seconds


async def _fetch_live_pricing() -> list[str]:
    """Probe provider endpoints for newly-released models the bundle lacks.

    This is a DRIFT DETECTOR, not a price writer: provider public endpoints
    expose model IDs, not machine-readable per-token prices, so it cannot set
    prices honestly. It surfaces models the bundle is missing so a human can
    add them. Caller gates this behind ``pricing_live_fetch_enabled``.
    """
    import asyncio

    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, _probe_openai_models
        )
    except Exception as exc:
        logger.debug("OpenAI model probe failed: %s", exc)
        return []


def _probe_openai_models() -> list[str]:
    """Return OpenAI model IDs not present in the bundle. Thread-safe."""
    try:
        req = urllib_request.Request(
            "https://api.openai.com/v1/models",
            headers={"User-Agent": "Modus/2.0"},
        )
        with urllib_request.urlopen(req, timeout=_PRICING_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read())
        models = [m.get("id", "") for m in data.get("data", [])]
        known = {k[1] for k in BUNDLED_PRICING if k[0] == "openai"}
        unknown = [m for m in models if m not in known and "gpt" in m.lower()]
        if unknown:
            logger.warning(
                "New OpenAI models not in bundled pricing (add them to "
                "BUNDLED_PRICING and pricing.py): %s", ", ".join(unknown[:10]),
            )
        return unknown
    except (URLError, Exception):
        return []
