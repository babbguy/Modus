"""
Tests for orchestrator.core.pricing_sync — bundled pricing data and sync logic.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.core.pricing_sync import (
    BUNDLED_PRICING,
    _probe_openai_models,
    sync_pricing,
)
from orchestrator.db.models import Base


@pytest_asyncio.fixture
async def db_session_pricing():
    """A fresh engine + sessionmaker + open session for pricing sync tests.

    StaticPool shares one in-memory DB across every connection, so the session
    used for assertions sees what sync_pricing's own session commits.
    """
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield factory, session
    await eng.dispose()


# ── BUNDLED_PRICING ──────────────────────────────────────────────────────────


class TestBundledPricing:
    def test_non_empty(self):
        assert len(BUNDLED_PRICING) > 0

    def test_all_keys_are_tuples(self):
        for key in BUNDLED_PRICING:
            assert isinstance(key, tuple)
            assert len(key) == 3
            provider, model, resource_type = key
            assert isinstance(provider, str) and provider
            assert isinstance(model, str) and model
            assert isinstance(resource_type, str) and resource_type

    def test_all_values_are_cost_pairs(self):
        for key, val in BUNDLED_PRICING.items():
            assert isinstance(val, tuple), f"Value for {key} is not a tuple"
            assert len(val) == 2
            input_cost, output_cost = val
            assert isinstance(input_cost, (int, float))
            assert isinstance(output_cost, (int, float))
            assert input_cost >= 0
            assert output_cost >= 0

    def test_major_providers_present(self):
        providers = {k[0] for k in BUNDLED_PRICING}
        assert "openai" in providers
        assert "anthropic" in providers
        assert "gcp" in providers

    def test_major_models_present(self):
        models = {k[1] for k in BUNDLED_PRICING}
        assert "gpt-4o" in models
        assert "claude-sonnet-4-5" in models

    def test_embedding_models(self):
        embeddings = {k for k in BUNDLED_PRICING if k[2] == "embedding"}
        assert len(embeddings) > 0


# ── sync_pricing ─────────────────────────────────────────────────────────────


class TestSyncPricing:
    @pytest.mark.asyncio
    async def test_no_session_factory(self):
        """sync_pricing returns early when no DB is available."""
        with patch("orchestrator.core.pricing_sync._session_factory", None):
            await sync_pricing()  # should not raise


# ── live probe ───────────────────────────────────────────────────────────────


class TestProbeOpenaiModels:
    def test_returns_empty_on_network_error(self):
        """Model probe returns [] when network is unavailable."""
        with patch("urllib.request.urlopen", side_effect=Exception("no network")):
            assert _probe_openai_models() == []


# ── Regression tests: versioned self-correction + air-gap egress gate ────────────


class TestPricingIntegrity:
    @pytest.mark.asyncio
    async def test_changed_price_produces_new_effective_version(self, db_session_pricing):
        """A changed bundled price inserts a newer-effective row that the
        latest-effective lookup returns — prices self-correct."""
        from decimal import Decimal
        from sqlalchemy import select
        from orchestrator.core import pricing_sync as ps
        from orchestrator.db.models import PricingModel

        factory, session = db_session_pricing
        key = ("openai", "gpt-4o", "llm_call")

        # First sync with the real bundle.
        with patch.object(ps, "_session_factory", factory):
            await ps.sync_pricing()

        async def _latest():
            return (await session.execute(
                select(PricingModel).where(
                    PricingModel.provider == "openai", PricingModel.model == "gpt-4o"
                ).order_by(PricingModel.effective_from.desc()).limit(1)
            )).scalar_one_or_none()

        first = await _latest()
        assert first is not None
        rows_before = len((await session.execute(
            select(PricingModel).where(PricingModel.model == "gpt-4o")
        )).scalars().all())

        # Simulate a price change in the bundle and re-sync.
        with patch.dict(ps.BUNDLED_PRICING, {key: (0.99, 1.98)}), \
                patch.object(ps, "_session_factory", factory):
            await ps.sync_pricing()

        latest = await _latest()
        assert latest.input_cost_per_1k == Decimal("0.99")
        assert latest.output_cost_per_1k == Decimal("1.98")
        assert latest.effective_from > first.effective_from
        assert latest.source == "bundled_pricing_v2_corrected"

        # A new version was added, not an in-place mutation (history preserved).
        rows_after = len((await session.execute(
            select(PricingModel).where(PricingModel.model == "gpt-4o")
        )).scalars().all())
        assert rows_after == rows_before + 1

    @pytest.mark.asyncio
    async def test_resync_unchanged_is_idempotent(self, db_session_pricing):
        from sqlalchemy import select, func
        from orchestrator.core import pricing_sync as ps
        from orchestrator.db.models import PricingModel

        factory, session = db_session_pricing
        with patch.object(ps, "_session_factory", factory):
            await ps.sync_pricing()
        n1 = (await session.execute(select(func.count(PricingModel.id)))).scalar()
        with patch.object(ps, "_session_factory", factory):
            await ps.sync_pricing()
        n2 = (await session.execute(select(func.count(PricingModel.id)))).scalar()
        assert n1 == n2, "re-syncing unchanged prices must not add rows"

    @pytest.mark.asyncio
    async def test_air_gap_makes_zero_egress(self, db_session_pricing, monkeypatch):
        """With live fetch disabled (default), sync performs no network call."""
        from orchestrator.core import pricing_sync as ps

        factory, _ = db_session_pricing
        monkeypatch.setattr(ps.settings, "pricing_live_fetch_enabled", False)

        called = {"urlopen": 0}

        def _boom(*a, **k):
            called["urlopen"] += 1
            raise AssertionError("network egress attempted in air-gap mode")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        with patch.object(ps, "_session_factory", factory):
            await ps.sync_pricing()
        assert called["urlopen"] == 0
