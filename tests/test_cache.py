"""
Tests for conductor.core.cache — Tiered Cache
"""
from __future__ import annotations

import asyncio

import pytest

from conductor.core.cache import TieredCache, CacheTier


# ── Basic get/set ────────────────────────────────────────────────────────────


class TestBasicOperations:
    @pytest.mark.asyncio
    async def test_set_and_get(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        await cache.set("key1", {"data": 42})
        result = await cache.get("key1")
        assert result == {"data": 42}

    @pytest.mark.asyncio
    async def test_get_missing_key(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        result = await cache.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_overwrite_existing_key(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        await cache.set("key1", "first")
        await cache.set("key1", "second")
        result = await cache.get("key1")
        assert result == "second"


# ── TTL expiry ───────────────────────────────────────────────────────────────


class TestTTLExpiry:
    @pytest.mark.asyncio
    async def test_hot_tier_expires(self):
        cache = TieredCache(hot_ttl=1, warm_ttl=300, cold_ttl=900)
        await cache.set("key1", "value", tier=CacheTier.HOT)

        result = await cache.get("key1")
        assert result == "value"

        # Wait for expiry
        await asyncio.sleep(1.1)
        result = await cache.get("key1")
        assert result is None

    @pytest.mark.asyncio
    async def test_warm_tier_longer_than_hot(self):
        cache = TieredCache(hot_ttl=1, warm_ttl=3, cold_ttl=900)
        await cache.set("hot_key", "hot_val", tier=CacheTier.HOT)
        await cache.set("warm_key", "warm_val", tier=CacheTier.WARM)

        await asyncio.sleep(1.1)

        # Hot should be expired
        assert await cache.get("hot_key") is None
        # Warm should still be valid
        assert await cache.get("warm_key") == "warm_val"


# ── Tier invalidation ───────────────────────────────────────────────────────


class TestTierInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_single_key(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        await cache.set("key1", "val1")
        await cache.set("key2", "val2")

        await cache.invalidate("key1")

        assert await cache.get("key1") is None
        assert await cache.get("key2") == "val2"

    @pytest.mark.asyncio
    async def test_invalidate_tier(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        await cache.set("hot1", "v1", tier=CacheTier.HOT)
        await cache.set("hot2", "v2", tier=CacheTier.HOT)
        await cache.set("warm1", "v3", tier=CacheTier.WARM)

        await cache.invalidate_tier(CacheTier.HOT)

        assert await cache.get("hot1") is None
        assert await cache.get("hot2") is None
        assert await cache.get("warm1") == "v3"

    @pytest.mark.asyncio
    async def test_invalidate_all(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        await cache.set("key1", "v1", tier=CacheTier.HOT)
        await cache.set("key2", "v2", tier=CacheTier.WARM)
        await cache.set("key3", "v3", tier=CacheTier.COLD)

        await cache.invalidate_all()

        assert await cache.get("key1") is None
        assert await cache.get("key2") is None
        assert await cache.get("key3") is None


# ── Eviction ─────────────────────────────────────────────────────────────────


class TestEviction:
    @pytest.mark.asyncio
    async def test_evict_oldest_at_capacity(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900, max_entries=3)
        await cache.set("key1", "v1")
        await cache.set("key2", "v2")
        await cache.set("key3", "v3")
        # Adding 4th key should evict the oldest (key1)
        await cache.set("key4", "v4")

        assert await cache.get("key1") is None
        assert await cache.get("key4") == "v4"
        assert cache.stats["entries"] == 3


# ── Stats ────────────────────────────────────────────────────────────────────


class TestStats:
    @pytest.mark.asyncio
    async def test_hit_miss_tracking(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        await cache.set("key1", "v1")

        await cache.get("key1")     # hit
        await cache.get("key1")     # hit
        await cache.get("missing")  # miss

        stats = cache.stats
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["hit_rate"] == pytest.approx(2 / 3, rel=0.01)


# ── get_with_meta ────────────────────────────────────────────────────────────


class TestGetWithMeta:
    @pytest.mark.asyncio
    async def test_get_with_meta_returns_entry(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        await cache.set("key1", "value", completeness_pct=95.0)

        entry = await cache.get_with_meta("key1")
        assert entry is not None
        assert entry.value == "value"
        assert entry.completeness_pct == 95.0

    @pytest.mark.asyncio
    async def test_get_with_meta_missing(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900)
        entry = await cache.get_with_meta("missing")
        assert entry is None


# ── Concurrent access ────────────────────────────────────────────────────────


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_reads_writes(self):
        cache = TieredCache(hot_ttl=60, warm_ttl=300, cold_ttl=900, max_entries=100)

        async def writer(n):
            for i in range(20):
                await cache.set(f"key-{n}-{i}", f"val-{n}-{i}")

        async def reader(n):
            for i in range(20):
                await cache.get(f"key-{n}-{i}")

        tasks = [writer(i) for i in range(5)] + [reader(i) for i in range(5)]
        await asyncio.gather(*tasks)

        # Should not raise or deadlock
        assert cache.stats["entries"] <= 100
