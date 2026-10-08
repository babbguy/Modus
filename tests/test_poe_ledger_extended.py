"""
Tests for orchestrator.core.poe_ledger — Extended coverage for
SLH-DSA signing, chain cache management, hash decision, and sampling.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator.core.poe_ledger import (
    ChainEntry,
    _CHAIN_CACHE,
    _MAX_CACHE_ENTRIES,
    _update_cache,
    hash_decision,
    should_sample,
    sign_entry_slh_dsa,
)


# ── hash_decision ───────────────────────────────────────────────────────────


class TestHashDecision:
    def test_deterministic(self):
        h1 = hash_decision("allow", "OK", "app-1", 0.05)
        h2 = hash_decision("allow", "OK", "app-1", 0.05)
        assert h1 == h2

    def test_sha256_length(self):
        h = hash_decision("deny", "Budget exceeded", "app-2", 1.50)
        assert len(h) == 64  # SHA-256 hex

    def test_different_decisions_different_hashes(self):
        h1 = hash_decision("allow", "OK", "app-1", 0.05)
        h2 = hash_decision("deny", "Blocked", "app-1", 0.05)
        assert h1 != h2

    def test_different_costs_different_hashes(self):
        h1 = hash_decision("allow", "OK", "app-1", 0.05)
        h2 = hash_decision("allow", "OK", "app-1", 1.00)
        assert h1 != h2

    def test_different_apps_different_hashes(self):
        h1 = hash_decision("allow", "OK", "app-1", 0.05)
        h2 = hash_decision("allow", "OK", "app-2", 0.05)
        assert h1 != h2


# ── ChainEntry extended ────────────────────────────────────────────────────


class TestChainEntryExtended:
    def test_genesis_entry(self):
        entry = ChainEntry(
            team_id="t1", session_id="s1", seq_num=0,
            prev_hash=None, decision_hash="abc",
            trajectory_proof_id=None,
            proof_type="hash_chain", risk_level="low",
        )
        h = entry.compute_hash()
        assert len(h) == 64

    def test_chained_entry(self):
        genesis = ChainEntry(
            team_id="t1", session_id="s1", seq_num=0,
            prev_hash=None, decision_hash="abc",
            trajectory_proof_id=None,
            proof_type="hash_chain", risk_level="low",
        )
        genesis_hash = genesis.compute_hash()

        second = ChainEntry(
            team_id="t1", session_id="s1", seq_num=1,
            prev_hash=genesis_hash, decision_hash="def",
            trajectory_proof_id=None,
            proof_type="hash_chain", risk_level="medium",
        )
        h2 = second.compute_hash()
        assert h2 != genesis_hash

    def test_different_risk_levels(self):
        base = dict(
            team_id="t1", session_id="s1", seq_num=0,
            prev_hash=None, decision_hash="abc",
            trajectory_proof_id=None, proof_type="hash_chain",
        )
        h_low = ChainEntry(**base, risk_level="low").compute_hash()
        h_high = ChainEntry(**base, risk_level="high").compute_hash()
        assert h_low != h_high

    def test_proof_type_affects_hash(self):
        base = dict(
            team_id="t1", session_id="s1", seq_num=0,
            prev_hash=None, decision_hash="abc",
            trajectory_proof_id=None, risk_level="medium",
        )
        h1 = ChainEntry(**base, proof_type="hash_chain").compute_hash()
        h2 = ChainEntry(**base, proof_type="snark").compute_hash()
        assert h1 != h2

    def test_frozen_dataclass(self):
        entry = ChainEntry(
            team_id="t1", session_id="s1", seq_num=0,
            prev_hash=None, decision_hash="abc",
            trajectory_proof_id=None,
            proof_type="hash_chain", risk_level="low",
        )
        with pytest.raises(AttributeError):
            entry.team_id = "t2"


# ── Chain cache ─────────────────────────────────────────────────────────────


class TestChainCache:
    def setup_method(self):
        _CHAIN_CACHE.clear()

    def test_update_creates_team_entry(self):
        _update_cache("team-1", 0, "hash-0")
        assert "team-1" in _CHAIN_CACHE
        assert len(_CHAIN_CACHE["team-1"]) == 1
        assert _CHAIN_CACHE["team-1"][-1] == (0, "hash-0")

    def test_update_appends(self):
        _update_cache("team-1", 0, "hash-0")
        _update_cache("team-1", 1, "hash-1")
        assert len(_CHAIN_CACHE["team-1"]) == 2

    def test_bounded_by_max_entries(self):
        for i in range(1200):
            _update_cache("team-1", i, f"hash-{i}")
        assert len(_CHAIN_CACHE["team-1"]) <= _MAX_CACHE_ENTRIES

    def test_separate_teams(self):
        _update_cache("team-a", 0, "ha")
        _update_cache("team-b", 0, "hb")
        assert len(_CHAIN_CACHE) == 2


# ── should_sample ───────────────────────────────────────────────────────────


class TestShouldSample:
    def test_disabled_returns_false(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_settings:
            mock_settings.poe_ledger_enabled = False
            assert should_sample("high") is False

    def test_high_risk_always_sampled_when_configured(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_settings:
            mock_settings.poe_ledger_enabled = True
            mock_settings.poe_high_risk_always_prove = True
            mock_settings.poe_sample_rate = 0.0
            assert should_sample("high") is True

    def test_sample_rate_zero_medium_risk(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_settings:
            mock_settings.poe_ledger_enabled = True
            mock_settings.poe_high_risk_always_prove = False
            mock_settings.poe_sample_rate = 0.0
            assert should_sample("medium") is False

    def test_sample_rate_one_always_true(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_settings:
            mock_settings.poe_ledger_enabled = True
            mock_settings.poe_high_risk_always_prove = False
            mock_settings.poe_sample_rate = 1.0
            assert should_sample("medium") is True


# ── sign_entry_slh_dsa ─────────────────────────────────────────────────────


class TestSignEntrySLHDSA:
    def test_valid_signature(self):
        result = sign_entry_slh_dsa("abc123" * 10, b"x" * 32)
        assert "signature" in result
        assert result["algorithm"] == "slh-dsa-shim-v1"
        assert "signed_at" in result
        assert len(result["signature"]) == 128  # SHA-512 hex

    def test_deterministic(self):
        key = b"test-key-material-xxxx"
        sig1 = sign_entry_slh_dsa("hash1", key)
        sig2 = sign_entry_slh_dsa("hash1", key)
        assert sig1["signature"] == sig2["signature"]

    def test_different_hashes_different_signatures(self):
        key = b"test-key-material-xxxx"
        sig1 = sign_entry_slh_dsa("hash1", key)
        sig2 = sign_entry_slh_dsa("hash2", key)
        assert sig1["signature"] != sig2["signature"]

    def test_empty_entry_hash_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            sign_entry_slh_dsa("", b"x" * 32)

    def test_none_entry_hash_raises(self):
        with pytest.raises(ValueError):
            sign_entry_slh_dsa(None, b"x" * 32)

    def test_short_key_raises(self):
        with pytest.raises(ValueError, match="length >= 16"):
            sign_entry_slh_dsa("hash", b"short")

    def test_non_bytes_key_raises(self):
        with pytest.raises(ValueError):
            sign_entry_slh_dsa("hash", "not-bytes")
