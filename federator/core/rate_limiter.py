"""
Modus Federator — Rate Limiter
====================================
Token-bucket rate limiter keyed by identity fingerprint.
Same pattern as orchestrator/main.py rate limiter.

In-memory, no external dependencies. Resets on restart (acceptable
for a single-process service).
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """
    Thread-safe token-bucket rate limiter.

    Args:
        rate_per_hour: sustained rate (tokens added per hour)
        burst: max tokens that can accumulate
    """

    __slots__ = ("_rate_per_second", "_burst", "_buckets", "_lock")

    def __init__(self, rate_per_hour: int, burst: int = 5) -> None:
        self._rate_per_second = rate_per_hour / 3600.0
        self._burst = max(burst, 1)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """
        Check if a request from `key` should be allowed.

        Returns True and consumes a token if allowed.
        Returns False if rate limit exceeded.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._burst - 1, last_refill=now)
                self._buckets[key] = bucket
                return True

            # Refill tokens based on elapsed time
            elapsed = now - bucket.last_refill
            bucket.tokens = min(
                self._burst,
                bucket.tokens + elapsed * self._rate_per_second,
            )
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def cleanup(self, max_age_seconds: float = 7200.0) -> int:
        """Remove stale buckets older than max_age_seconds. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            stale = [
                k for k, b in self._buckets.items()
                if (now - b.last_refill) > max_age_seconds
            ]
            for k in stale:
                del self._buckets[k]
            return len(stale)
