"""
Tests for orchestrator.core.poe_ledger —
Hash chain computation, verification, sampling, and cache management.
"""
from __future__ import annotations


from orchestrator.core.poe_ledger import (
    ChainEntry,
    hash_decision,
    should_sample,
    _update_cache,
    _CHAIN_CACHE,
)


class TestChainEntry:
    """Hash chain entry computation."""

    def test_compute_hash_deterministic(self):
        """Same inputs must produce same hash."""
        entry = ChainEntry(
            team_id="team-1",
            session_id="sess-1",
            seq_num=0,
            prev_hash=None,
            decision_hash="abc123",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="medium",
        )
        h1 = entry.compute_hash()
        h2 = entry.compute_hash()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_compute_hash_differs_on_input_change(self):
        """Different inputs must produce different hashes."""
        base = dict(
            team_id="team-1",
            session_id="sess-1",
            seq_num=0,
            prev_hash=None,
            decision_hash="abc123",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="medium",
        )
        h1 = ChainEntry(**base).compute_hash()
        h2 = ChainEntry(**{**base, "seq_num": 1}).compute_hash()
        assert h1 != h2

    def test_compute_hash_chain_link(self):
        """Entry with prev_hash should produce different hash than genesis."""
        genesis = ChainEntry(
            team_id="t", session_id="s", seq_num=0,
            prev_hash=None, decision_hash="d",
            trajectory_proof_id=None, proof_type="hash_chain",
            risk_level="medium",
        )
        genesis_hash = genesis.compute_hash()

        linked = ChainEntry(
            team_id="t", session_id="s", seq_num=1,
            prev_hash=genesis_hash, decision_hash="d",
            trajectory_proof_id=None, proof_type="hash_chain",
            risk_level="medium",
        )
        assert linked.compute_hash() != genesis_hash


class TestHashDecision:
    """Decision hashing."""

    def test_hash_decision_deterministic(self):
        h1 = hash_decision("allow", "ok", "app-1", 0.05)
        h2 = hash_decision("allow", "ok", "app-1", 0.05)
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_decision_differs(self):
        h1 = hash_decision("allow", "ok", "app-1", 0.05)
        h2 = hash_decision("deny", "budget", "app-1", 0.05)
        assert h1 != h2


class TestSampling:
    """Sampling logic."""

    def test_should_sample_disabled(self, monkeypatch):
        """When disabled, never sample."""
        monkeypatch.setattr("orchestrator.core.poe_ledger.settings.poe_ledger_enabled", False)
        assert should_sample() is False

    def test_should_sample_high_risk_always(self, monkeypatch):
        """High-risk always sampled when configured."""
        monkeypatch.setattr("orchestrator.core.poe_ledger.settings.poe_ledger_enabled", True)
        monkeypatch.setattr("orchestrator.core.poe_ledger.settings.poe_high_risk_always_prove", True)
        assert should_sample("high") is True

    def test_should_sample_rate_zero(self, monkeypatch):
        """With rate=0.0, non-high-risk should never sample."""
        monkeypatch.setattr("orchestrator.core.poe_ledger.settings.poe_ledger_enabled", True)
        monkeypatch.setattr("orchestrator.core.poe_ledger.settings.poe_high_risk_always_prove", False)
        monkeypatch.setattr("orchestrator.core.poe_ledger.settings.poe_sample_rate", 0.0)
        # With rate 0.0, random() < 0.0 is always False
        assert should_sample("medium") is False


class TestCache:
    """Chain cache management."""

    def test_update_cache_creates_entry(self):
        _CHAIN_CACHE.clear()
        _update_cache("team-test", 0, "hash-0")
        assert "team-test" in _CHAIN_CACHE
        assert len(_CHAIN_CACHE["team-test"]) == 1
        assert _CHAIN_CACHE["team-test"][-1] == (0, "hash-0")

    def test_update_cache_appends(self):
        _CHAIN_CACHE.clear()
        _update_cache("team-test", 0, "hash-0")
        _update_cache("team-test", 1, "hash-1")
        assert len(_CHAIN_CACHE["team-test"]) == 2
        assert _CHAIN_CACHE["team-test"][-1] == (1, "hash-1")

    def test_cache_bounded(self):
        """Cache should not exceed _MAX_CACHE_ENTRIES."""
        _CHAIN_CACHE.clear()
        for i in range(1100):
            _update_cache("team-bounded", i, f"hash-{i}")
        assert len(_CHAIN_CACHE["team-bounded"]) <= 1000
