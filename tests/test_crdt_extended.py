"""
Tests for orchestrator.core.crdt_sync — Extended coverage for gossip protocol,
delta encoding/decoding, CRDTSyncNode operations, and AppBudgetCRDT.
"""
from __future__ import annotations

import struct

import pytest

from orchestrator.core.crdt_sync import (
    CRDT_MAGIC,
    AppBudgetCRDT,
    CRDTSyncNode,
    LWWRegister,
    MergeStrategy,
    decode_delta,
    encode_delta,
)


# ── AppBudgetCRDT ────────────────────────────────────────────────────────────


class TestAppBudgetCRDT:
    def test_is_over_budget_no_limit(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.spend_cents.increment("n1", 1000)
        assert crdt.is_over_budget() is False

    def test_is_over_budget_below(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(1000, "n1")
        crdt.spend_cents.increment("n1", 500)
        assert crdt.is_over_budget() is False

    def test_is_over_budget_at_limit(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(1000, "n1")
        crdt.spend_cents.increment("n1", 1000)
        assert crdt.is_over_budget() is True

    def test_is_over_budget_above(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(1000, "n1")
        crdt.spend_cents.increment("n1", 1500)
        assert crdt.is_over_budget() is True

    def test_remaining_cents_no_limit(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        assert crdt.remaining_cents() == 999999999

    def test_remaining_cents_with_spend(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(1000, "n1")
        crdt.spend_cents.increment("n1", 300)
        assert crdt.remaining_cents() == 700

    def test_remaining_cents_zero_floor(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(100, "n1")
        crdt.spend_cents.increment("n1", 500)
        assert crdt.remaining_cents() == 0

    def test_merge(self):
        crdt1 = AppBudgetCRDT(app_id="app-1")
        crdt1.spend_cents.increment("n1", 100)
        crdt1.call_count.increment("n1", 5)

        crdt2 = AppBudgetCRDT(app_id="app-1")
        crdt2.spend_cents.increment("n2", 200)
        crdt2.call_count.increment("n2", 3)

        merged = crdt1.merge(crdt2)
        assert merged.spend_cents.value == 300
        assert merged.call_count.value == 8

    def test_to_dict_and_back(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.spend_cents.increment("n1", 100)
        crdt.call_count.increment("n1", 5)
        crdt.budget_limit_cents.set(5000, "n1")
        crdt.policy_version.set("v1.2.3", "n1")

        data = crdt.to_dict()
        restored = AppBudgetCRDT.from_dict(data)
        assert restored.app_id == "app-1"
        assert restored.spend_cents.value == 100
        assert restored.call_count.value == 5
        assert restored.budget_limit_cents.value == 5000
        assert restored.policy_version.value == "v1.2.3"

    def test_pn_counter_refunds(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(1000, "n1")
        crdt.spend_cents.increment("n1", 800)
        assert crdt.is_over_budget() is False
        crdt.spend_cents.increment("n1", 300)
        assert crdt.is_over_budget() is True
        crdt.spend_cents.decrement("n1", 200)  # refund
        assert crdt.spend_cents.value == 900
        assert crdt.is_over_budget() is False


# ── Delta encoding/decoding ──────────────────────────────────────────────────


class TestDeltaEncoding:
    def test_roundtrip(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.spend_cents.increment("n1", 500)
        crdt.call_count.increment("n1", 10)
        crdt.budget_limit_cents.set(10000, "n1")

        states = {"app-1": crdt}
        encoded = encode_delta("node-1", states)
        decoded_node, decoded_states = decode_delta(encoded)

        assert decoded_node == "node-1"
        assert "app-1" in decoded_states
        assert decoded_states["app-1"].spend_cents.value == 500
        assert decoded_states["app-1"].call_count.value == 10

    def test_multiple_apps(self):
        crdt1 = AppBudgetCRDT(app_id="app-1")
        crdt1.spend_cents.increment("n1", 100)
        crdt2 = AppBudgetCRDT(app_id="app-2")
        crdt2.spend_cents.increment("n1", 200)

        states = {"app-1": crdt1, "app-2": crdt2}
        encoded = encode_delta("node-x", states)
        node_id, decoded = decode_delta(encoded)

        assert node_id == "node-x"
        assert len(decoded) == 2
        assert decoded["app-1"].spend_cents.value == 100
        assert decoded["app-2"].spend_cents.value == 200

    def test_empty_states(self):
        encoded = encode_delta("empty-node", {})
        node_id, states = decode_delta(encoded)
        assert node_id == "empty-node"
        assert len(states) == 0

    def test_invalid_magic_raises(self):
        bad_data = b"XXXX" + b"\x00" * 50
        with pytest.raises(ValueError, match="Invalid CRDT packet magic"):
            decode_delta(bad_data)

    def test_invalid_version_raises(self):
        bad_data = CRDT_MAGIC + struct.pack("!B", 99) + b"\x00" * 50
        with pytest.raises(ValueError, match="Unsupported CRDT version"):
            decode_delta(bad_data)


# ── CRDTSyncNode ────────────────────────────────────────────────────────────


class TestCRDTSyncNode:
    def test_record_spend_creates_state(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        result = node.record_spend("app-1", 100)
        assert result is True
        state = node.get_state("app-1")
        assert state is not None
        assert state.spend_cents.value == 100

    def test_record_spend_accumulates(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.record_spend("app-1", 100)
        node.record_spend("app-1", 200)
        state = node.get_state("app-1")
        assert state.spend_cents.value == 300

    def test_record_spend_denied_over_budget(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("app-1", 500)
        assert node.record_spend("app-1", 400) is True
        # Now at 400/500
        assert node.record_spend("app-1", 200) is True  # 600 > 500 but spend is recorded before check
        # Budget is now 600, which exceeds 500
        assert node.record_spend("app-1", 1) is False  # should be denied

    def test_check_budget_unknown_app(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        allowed, remaining = node.check_budget("unknown-app")
        assert allowed is True
        assert remaining == 999999999

    def test_check_budget_known_app(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("app-1", 1000)
        node.record_spend("app-1", 300)
        allowed, remaining = node.check_budget("app-1")
        assert allowed is True
        assert remaining == 700

    def test_set_budget_limit(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("app-1", 5000)
        state = node.get_state("app-1")
        assert state.budget_limit_cents.value == 5000

    def test_get_all_states_empty(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        states = node.get_all_states()
        assert len(states) == 0

    def test_get_all_states_multiple(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.record_spend("app-1", 100)
        node.record_spend("app-2", 200)
        states = node.get_all_states()
        assert len(states) == 2

    def test_node_id_generation(self):
        node = CRDTSyncNode(gossip_port=0)
        assert len(node._node_id) == 12  # sha256[:12]

    def test_stats_tracking(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.record_spend("app-1", 100)
        node.record_spend("app-1", 200)
        assert node._stats["local_checks"] == 2

    def test_stop_idempotent(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.stop()  # Should not raise even if never started
        node.stop()


# ── LWWRegister tie-breaking ─────────────────────────────────────────────────


class TestLWWRegisterTieBreaking:
    def test_higher_timestamp_wins(self):
        r1 = LWWRegister(value="old", timestamp=1.0, node_id="a")
        r2 = LWWRegister(value="new", timestamp=2.0, node_id="b")
        merged = r1.merge(r2)
        assert merged.value == "new"

    def test_same_timestamp_higher_node_wins(self):
        r1 = LWWRegister(value="a-val", timestamp=1.0, node_id="a")
        r2 = LWWRegister(value="b-val", timestamp=1.0, node_id="b")
        merged = r1.merge(r2)
        assert merged.value == "b-val"

    def test_self_wins_on_equal(self):
        r1 = LWWRegister(value="same", timestamp=1.0, node_id="a")
        r2 = LWWRegister(value="other", timestamp=0.5, node_id="z")
        merged = r1.merge(r2)
        assert merged.value == "same"


# ── MergeStrategy enum ──────────────────────────────────────────────────────


class TestMergeStrategy:
    def test_values(self):
        assert MergeStrategy.MAX == "max"
        assert MergeStrategy.SUM == "sum"
        assert MergeStrategy.LWW == "lww"
