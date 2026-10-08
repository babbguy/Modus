"""
Modus Conductor — Tiered Cache
=====================================
In-memory cache with three tiers for dashboard performance:

  Hot  (60s):  KPI cards, current burn rate — data that changes frequently
  Warm (5m):   Charts, team breakdowns — data that updates periodically
  Cold (15m):  Historical reports, audit data — rarely changes

Each cache entry tracks:
  - The cached value
  - When it was cached (for TTL expiry)
  - A completeness percentage (from reconciliation)

Thread-safe via asyncio locks.

Four Laws compliance:
  - Zero external deps (no Redis, no Memcached — pure stdlib)
  - Works on $5/mo VPS (memory-bounded cache)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from conductor.core.config import settings

logger = logging.getLogger(__name__)


class CacheTier(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass
class CacheEntry:
    value: Any
    cached_at: float  # monotonic time
    completeness_pct: float = 100.0
    hit_count: int = 0


class TieredCache:
    """In-memory tiered cache with TTL-based expiry."""

    def __init__(
        self,
        hot_ttl: int | None = None,
        warm_ttl: int | None = None,
        cold_ttl: int | None = None,
        max_entries: int = 1000,
    ):
        self._hot_ttl = hot_ttl or settings.cache_hot_ttl_seconds
        self._warm_ttl = warm_ttl or settings.cache_warm_ttl_seconds
        self._cold_ttl = cold_ttl or settings.cache_cold_ttl_seconds
        self._max_entries = max_entries

        self._store: dict[str, CacheEntry] = {}
        self._tiers: dict[str, CacheTier] = {}
        self._lock = asyncio.Lock()

        # Stats
        self._hits = 0
        self._misses = 0

    def _ttl_for_tier(self, tier: CacheTier) -> int:
        if tier == CacheTier.HOT:
            return self._hot_ttl
        elif tier == CacheTier.WARM:
            return self._warm_ttl
        return self._cold_ttl

    async def get(self, key: str) -> Optional[Any]:
        """Get a value from cache. Returns None if expired or missing."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None

            tier = self._tiers.get(key, CacheTier.WARM)
            ttl = self._ttl_for_tier(tier)
            age = time.monotonic() - entry.cached_at

            if age > ttl:
                # Expired
                del self._store[key]
                del self._tiers[key]
                self._misses += 1
                return None

            entry.hit_count += 1
            self._hits += 1
            return entry.value

    async def get_with_meta(self, key: str) -> Optional[CacheEntry]:
        """Get cache entry with metadata (completeness, age)."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None

            tier = self._tiers.get(key, CacheTier.WARM)
            ttl = self._ttl_for_tier(tier)
            age = time.monotonic() - entry.cached_at

            if age > ttl:
                del self._store[key]
                del self._tiers[key]
                return None

            return entry

    async def set(
        self,
        key: str,
        value: Any,
        tier: CacheTier = CacheTier.WARM,
        completeness_pct: float = 100.0,
    ) -> None:
        """Store a value in cache with the specified tier."""
        async with self._lock:
            # Evict if at capacity
            if len(self._store) >= self._max_entries and key not in self._store:
                self._evict_oldest()

            self._store[key] = CacheEntry(
                value=value,
                cached_at=time.monotonic(),
                completeness_pct=completeness_pct,
            )
            self._tiers[key] = tier

    async def invalidate(self, key: str) -> None:
        """Remove a specific key from cache."""
        async with self._lock:
            self._store.pop(key, None)
            self._tiers.pop(key, None)

    async def invalidate_tier(self, tier: CacheTier) -> None:
        """Invalidate all entries in a specific tier."""
        async with self._lock:
            keys_to_remove = [k for k, t in self._tiers.items() if t == tier]
            for k in keys_to_remove:
                del self._store[k]
                del self._tiers[k]

    async def invalidate_all(self) -> None:
        """Clear the entire cache."""
        async with self._lock:
            self._store.clear()
            self._tiers.clear()

    def _evict_oldest(self) -> None:
        """Evict the oldest entry. Called under lock."""
        if not self._store:
            return
        oldest_key = min(self._store, key=lambda k: self._store[k].cached_at)
        del self._store[oldest_key]
        self._tiers.pop(oldest_key, None)

    @property
    def stats(self) -> dict:
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / max(self._hits + self._misses, 1),
        }


# Module-level singleton
cache = TieredCache()
