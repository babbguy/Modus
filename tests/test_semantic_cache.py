"""
Tests for sdk.modus.semantic_cache — cosine similarity, embedding blob
round-trip, hashing, message text extraction, CacheDB operations, and
SemanticCache exact-match behaviour.
"""

from __future__ import annotations

import os
import sys

# Ensure the SDK package is importable
_sdk_path = os.path.join(os.path.dirname(__file__), os.pardir, "sdk")
if _sdk_path not in sys.path:
    sys.path.insert(0, os.path.abspath(_sdk_path))

import pytest

from modus.semantic_cache import (
    SemanticCache,
    _CacheDB,
    _blob_to_embedding,
    _cosine_similarity,
    _embedding_to_blob,
    _hash_messages,
    _messages_to_text,
)


# ── Cosine Similarity ──────────────────────────────────────────────────────


class TestCosineSimilarity:
    def test_cosine_similarity_identical(self):
        v = [1.0, 2.0, 3.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-9)

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-9)

    def test_cosine_similarity_empty(self):
        assert _cosine_similarity([], []) == 0.0


# ── Embedding Blob Round-Trip ───────────────────────────────────────────────


class TestEmbeddingBlob:
    def test_embedding_blob_roundtrip(self):
        original = [0.1, 0.25, -0.5, 3.14, 0.0]
        blob = _embedding_to_blob(original)
        recovered = _blob_to_embedding(blob)
        assert len(recovered) == len(original)
        for a, b in zip(original, recovered):
            assert a == pytest.approx(b, abs=1e-6)


# ── Hash Messages ──────────────────────────────────────────────────────────


class TestHashMessages:
    def test_hash_messages_deterministic(self):
        h1 = _hash_messages("openai", "gpt-4o", "hello world")
        h2 = _hash_messages("openai", "gpt-4o", "hello world")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_hash_messages_different_inputs(self):
        h1 = _hash_messages("openai", "gpt-4o", "hello")
        h2 = _hash_messages("openai", "gpt-4o", "goodbye")
        assert h1 != h2


# ── Messages to Text ───────────────────────────────────────────────────────


class TestMessagesToText:
    def test_messages_to_text_string(self):
        assert _messages_to_text("plain text") == "plain text"

    def test_messages_to_text_list(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        text = _messages_to_text(messages)
        assert "Hello" in text
        assert "Hi there" in text


# ── CacheDB ────────────────────────────────────────────────────────────────


class TestCacheDB:
    def test_cache_db_put_and_get_exact(self, tmp_path):
        db = _CacheDB(str(tmp_path / "cache.db"))
        try:
            db.put("hash1", "openai", "gpt-4o", {"response": "hello"})
            result = db.get_exact("hash1")
            assert result == {"response": "hello"}
        finally:
            db.close()

    def test_cache_db_lru_eviction(self, tmp_path):
        db = _CacheDB(str(tmp_path / "cache.db"), max_entries=3)
        try:
            for i in range(5):
                db.put(f"hash{i}", "openai", "gpt-4o", {"i": i})

            stats = db.stats()
            assert stats["entries"] <= 3

            # The earliest entries should have been evicted
            assert db.get_exact("hash0") is None
            assert db.get_exact("hash1") is None
        finally:
            db.close()

    def test_cache_db_stats(self, tmp_path):
        db = _CacheDB(str(tmp_path / "cache.db"))
        try:
            db.put("h1", "openai", "gpt-4o", {"a": 1})
            db.put("h2", "openai", "gpt-4o", {"b": 2})

            stats = db.stats()
            assert stats["entries"] == 2
            assert stats["semantic_entries"] == 0  # no embeddings stored
        finally:
            db.close()


# ── SemanticCache (exact-match tier only, no ONNX model) ────────────────────


class TestSemanticCache:
    def test_semantic_cache_exact_hit(self, tmp_path):
        cache = SemanticCache(cache_db_path=str(tmp_path / "cache.db"))
        try:
            cache.put("openai", "gpt-4o", "hello", {"text": "world"})
            result = cache.get("openai", "gpt-4o", "hello")
            assert result == {"text": "world"}
        finally:
            cache.close()

    def test_semantic_cache_miss(self, tmp_path):
        cache = SemanticCache(cache_db_path=str(tmp_path / "cache.db"))
        try:
            result = cache.get("openai", "gpt-4o", "not cached")
            assert result is None
        finally:
            cache.close()

    def test_semantic_cache_stats(self, tmp_path):
        cache = SemanticCache(cache_db_path=str(tmp_path / "cache.db"))
        try:
            cache.put("openai", "gpt-4o", "msg1", {"r": 1})
            cache.get("openai", "gpt-4o", "msg1")  # hit
            cache.get("openai", "gpt-4o", "msg2")  # miss

            stats = cache.stats()
            assert stats["hits"] == 1
            assert stats["misses"] == 1
            assert stats["entries"] == 1
        finally:
            cache.close()
