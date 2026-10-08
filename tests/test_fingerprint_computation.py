"""Tests for routing fingerprint computation — determinism, collision resistance."""

from modus.routing_interceptor import (
    compute_fingerprint,
    compute_system_prompt_hash,
    token_bucket,
    BUCKET_BOUNDARIES,
)


class TestComputeFingerprint:
    def test_deterministic(self):
        """Same inputs produce same fingerprint every time."""
        fp1 = compute_fingerprint("app-1", "You are a helpful assistant", "classify")
        fp2 = compute_fingerprint("app-1", "You are a helpful assistant", "classify")
        assert fp1 == fp2
        assert len(fp1) == 32  # SHA256 truncated to 32 hex chars

    def test_different_apps_different_fingerprints(self):
        prompt = "You are a helpful assistant"
        fp1 = compute_fingerprint("app-1", prompt)
        fp2 = compute_fingerprint("app-2", prompt)
        assert fp1 != fp2

    def test_different_prompts_different_fingerprints(self):
        fp1 = compute_fingerprint("app-1", "You are a classifier")
        fp2 = compute_fingerprint("app-1", "You are a summarizer")
        assert fp1 != fp2

    def test_different_call_sites_different_fingerprints(self):
        prompt = "You are a helpful assistant"
        fp1 = compute_fingerprint("app-1", prompt, "site-a")
        fp2 = compute_fingerprint("app-1", prompt, "site-b")
        assert fp1 != fp2

    def test_none_call_site_uses_default(self):
        prompt = "You are a helpful assistant"
        fp1 = compute_fingerprint("app-1", prompt, None)
        fp2 = compute_fingerprint("app-1", prompt)
        assert fp1 == fp2

    def test_empty_prompt(self):
        """Empty system prompt should still produce valid fingerprint."""
        fp = compute_fingerprint("app-1", "")
        assert len(fp) == 32

    def test_unicode_prompt(self):
        """Unicode content should hash correctly."""
        fp = compute_fingerprint("app-1", "你好世界 🌍")
        assert len(fp) == 32


class TestSystemPromptHash:
    def test_deterministic(self):
        h1 = compute_system_prompt_hash("test prompt")
        h2 = compute_system_prompt_hash("test prompt")
        assert h1 == h2
        assert len(h1) == 16

    def test_different_prompts(self):
        h1 = compute_system_prompt_hash("prompt A")
        h2 = compute_system_prompt_hash("prompt B")
        assert h1 != h2


class TestTokenBucket:
    def test_zero_tokens(self):
        assert token_bucket(0) == 1  # first bucket

    def test_small_count(self):
        assert token_bucket(50) == 1  # < 100

    def test_boundary_100(self):
        assert token_bucket(99) == 1
        assert token_bucket(100) == 2

    def test_medium_count(self):
        assert token_bucket(500) == 4  # 500 <= x < 1000

    def test_large_count(self):
        assert token_bucket(10000) == 8

    def test_very_large(self):
        assert token_bucket(100000) == 9  # last bucket

    def test_bucket_count_matches_boundaries(self):
        assert len(BUCKET_BOUNDARIES) == 10
