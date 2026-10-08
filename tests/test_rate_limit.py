"""
Tests — Rate limiter (under/over limit, per-key isolation, thread safety)
"""
from __future__ import annotations

import threading



def test_under_limit_allows():
    from orchestrator.main import _RateLimiter
    limiter = _RateLimiter(limit=10, window=60.0)
    for _ in range(10):
        assert limiter.check("test-key") is True


def test_over_limit_denies():
    from orchestrator.main import _RateLimiter
    limiter = _RateLimiter(limit=5, burst=0, window=60.0)
    for _ in range(5):
        assert limiter.check("test-key") is True
    assert limiter.check("test-key") is False


def test_per_key_isolation():
    from orchestrator.main import _RateLimiter
    limiter = _RateLimiter(limit=3, burst=0, window=60.0)
    for _ in range(3):
        limiter.check("key-a")
    # key-a is exhausted
    assert limiter.check("key-a") is False
    # key-b should still be allowed
    assert limiter.check("key-b") is True


def test_thread_safety():
    from orchestrator.main import _RateLimiter
    limiter = _RateLimiter(limit=1000, window=60.0)
    results = []

    def worker():
        for _ in range(100):
            results.append(limiter.check("shared"))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1000
    allowed = sum(1 for r in results if r)
    denied = sum(1 for r in results if not r)
    assert allowed == 1000
    assert denied == 0


def test_cleanup_removes_stale_keys():
    import time
    from orchestrator.main import _RateLimiter
    limiter = _RateLimiter(limit=10, window=0.01, cleanup_interval=0.0)
    limiter.check("stale-key")
    time.sleep(0.02)
    # Next check triggers cleanup
    limiter.check("fresh-key")
    with limiter._lock:
        assert "stale-key" not in limiter._counters
