"""Tests for orchestrator.core.poe_ledger — hash chain, Merkle, sampling."""
from unittest.mock import patch


from orchestrator.core.poe_ledger import (
    ChainEntry,
    _CHAIN_CACHE,
    _MAX_CACHE_ENTRIES,
    _update_cache,
    hash_decision,
    should_sample,
)


# ── ChainEntry ───────────────────────────────────────────────────────────────

class TestChainEntry:
    def test_compute_hash_deterministic(self):
        entry = ChainEntry(
            team_id="t1",
            session_id="s1",
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

    def test_compute_hash_changes_with_input(self):
        e1 = ChainEntry(
            team_id="t1",
            session_id="s1",
            seq_num=0,
            prev_hash=None,
            decision_hash="abc",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="medium",
        )
        e2 = ChainEntry(
            team_id="t1",
            session_id="s1",
            seq_num=0,
            prev_hash=None,
            decision_hash="xyz",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="medium",
        )
        assert e1.compute_hash() != e2.compute_hash()

    def test_compute_hash_with_prev_hash(self):
        entry = ChainEntry(
            team_id="t1",
            session_id="s1",
            seq_num=1,
            prev_hash="prev_hash_value",
            decision_hash="dec123",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="high",
        )
        h = entry.compute_hash()
        assert len(h) == 64

    def test_seq_num_affects_hash(self):
        args = dict(
            team_id="t1",
            session_id="s1",
            prev_hash=None,
            decision_hash="abc",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="medium",
        )
        e1 = ChainEntry(seq_num=0, **args)
        e2 = ChainEntry(seq_num=1, **args)
        assert e1.compute_hash() != e2.compute_hash()


# ── hash_decision ────────────────────────────────────────────────────────────

class TestHashDecision:
    def test_deterministic(self):
        h1 = hash_decision("allow", "ok", "app1", 0.5)
        h2 = hash_decision("allow", "ok", "app1", 0.5)
        assert h1 == h2

    def test_different_inputs(self):
        h1 = hash_decision("allow", "ok", "app1", 0.5)
        h2 = hash_decision("deny", "blocked", "app1", 0.5)
        assert h1 != h2

    def test_returns_sha256(self):
        h = hash_decision("allow", "ok", "app1", 0.0)
        assert len(h) == 64


# ── should_sample ────────────────────────────────────────────────────────────

class TestShouldSample:
    def test_disabled(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_s:
            mock_s.poe_ledger_enabled = False
            assert should_sample("high") is False

    def test_high_risk_always(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_s:
            mock_s.poe_ledger_enabled = True
            mock_s.poe_high_risk_always_prove = True
            mock_s.poe_sample_rate = 0.0
            assert should_sample("high") is True

    def test_sample_rate_zero(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_s:
            mock_s.poe_ledger_enabled = True
            mock_s.poe_high_risk_always_prove = False
            mock_s.poe_sample_rate = 0.0
            # With 0.0 sample rate, random() will always be >= 0.0
            assert should_sample("medium") is False

    def test_sample_rate_one(self):
        with patch("orchestrator.core.poe_ledger.settings") as mock_s:
            mock_s.poe_ledger_enabled = True
            mock_s.poe_high_risk_always_prove = False
            mock_s.poe_sample_rate = 1.0
            assert should_sample("medium") is True


# ── _update_cache ────────────────────────────────────────────────────────────

class TestUpdateCache:
    def test_creates_deque(self):
        team = "test-team-uc"
        _CHAIN_CACHE.pop(team, None)
        _update_cache(team, 0, "hash0")
        assert team in _CHAIN_CACHE
        assert len(_CHAIN_CACHE[team]) == 1
        _CHAIN_CACHE.pop(team, None)

    def test_appends(self):
        team = "test-team-uc2"
        _CHAIN_CACHE.pop(team, None)
        _update_cache(team, 0, "hash0")
        _update_cache(team, 1, "hash1")
        assert len(_CHAIN_CACHE[team]) == 2
        assert _CHAIN_CACHE[team][-1] == (1, "hash1")
        _CHAIN_CACHE.pop(team, None)

    def test_bounded(self):
        team = "test-team-bounded"
        _CHAIN_CACHE.pop(team, None)
        for i in range(_MAX_CACHE_ENTRIES + 100):
            _update_cache(team, i, f"h{i}")
        assert len(_CHAIN_CACHE[team]) == _MAX_CACHE_ENTRIES
        _CHAIN_CACHE.pop(team, None)


# ── _generate_snark_proof ────────────────────────────────────────────────────

class TestGenerateSnarkProof:
    def test_snark_proof_with_valid_entry(self):
        """Test SNARK proof generation returns 'snark' or 'hash_chain'."""
        from orchestrator.core.poe_ledger import _generate_snark_proof

        entry = ChainEntry(
            team_id="t1",
            session_id="s1",
            seq_num=5,
            prev_hash="abcdef12",
            decision_hash="12345678abcdef",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="medium",
        )
        h = entry.compute_hash()
        result = _generate_snark_proof(entry, h)
        assert result in ("snark", "hash_chain")

    def test_snark_proof_no_prev_hash(self):
        from orchestrator.core.poe_ledger import _generate_snark_proof

        entry = ChainEntry(
            team_id="t1",
            session_id="s1",
            seq_num=0,
            prev_hash=None,
            decision_hash="aabbccdd",
            trajectory_proof_id=None,
            proof_type="hash_chain",
            risk_level="low",
        )
        result = _generate_snark_proof(entry, entry.compute_hash())
        assert result in ("snark", "hash_chain")
