"""Tests for orchestrator.core.crdt_sync — CRDT local-first sync engine."""
import struct
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.crdt_sync import (
    AppBudgetCRDT,
    CRDTHub,
    CRDTSyncNode,
    GCounter,
    LWWRegister,
    MergeStrategy,
    PNCounter,
    decode_delta,
    encode_delta,
    CRDT_MAGIC,
)


# ── GCounter ─────────────────────────────────────────────────────────────────

class TestGCounter:
    def test_increment_single_node(self):
        gc = GCounter()
        gc.increment("node1", 5)
        assert gc.value == 5
        gc.increment("node1", 3)
        assert gc.value == 8

    def test_increment_multiple_nodes(self):
        gc = GCounter()
        gc.increment("a", 10)
        gc.increment("b", 20)
        assert gc.value == 30

    def test_merge_disjoint(self):
        a = GCounter(counts={"n1": 5})
        b = GCounter(counts={"n2": 10})
        merged = a.merge(b)
        assert merged.value == 15
        assert merged.counts["n1"] == 5
        assert merged.counts["n2"] == 10

    def test_merge_overlapping_takes_max(self):
        a = GCounter(counts={"n1": 5, "n2": 3})
        b = GCounter(counts={"n1": 2, "n2": 7})
        merged = a.merge(b)
        assert merged.counts["n1"] == 5
        assert merged.counts["n2"] == 7

    def test_to_dict_from_dict_roundtrip(self):
        gc = GCounter(counts={"x": 42, "y": 99})
        d = gc.to_dict()
        restored = GCounter.from_dict(d)
        assert restored.value == gc.value
        assert restored.counts == gc.counts

    def test_empty_counter(self):
        gc = GCounter()
        assert gc.value == 0


# ── PNCounter ────────────────────────────────────────────────────────────────

class TestPNCounter:
    def test_increment_and_value(self):
        pn = PNCounter()
        pn.increment("n1", 100)
        assert pn.value == 100

    def test_decrement(self):
        pn = PNCounter()
        pn.increment("n1", 100)
        pn.decrement("n1", 30)
        assert pn.value == 70

    def test_merge(self):
        a = PNCounter()
        a.increment("n1", 50)
        a.decrement("n1", 10)

        b = PNCounter()
        b.increment("n1", 60)
        b.decrement("n1", 5)

        merged = a.merge(b)
        # max of p.n1 = max(50,60)=60, max of n.n1 = max(10,5)=10
        assert merged.value == 50  # 60 - 10

    def test_to_dict_from_dict_roundtrip(self):
        pn = PNCounter()
        pn.increment("a", 100)
        pn.decrement("b", 25)
        d = pn.to_dict()
        restored = PNCounter.from_dict(d)
        assert restored.value == pn.value


# ── LWWRegister ──────────────────────────────────────────────────────────────

class TestLWWRegister:
    def test_set_and_read(self):
        reg = LWWRegister()
        reg.set("hello", "n1")
        assert reg.value == "hello"
        assert reg.node_id == "n1"

    def test_merge_higher_timestamp_wins(self):
        a = LWWRegister(value="old", timestamp=1.0, node_id="n1")
        b = LWWRegister(value="new", timestamp=2.0, node_id="n2")
        merged = a.merge(b)
        assert merged.value == "new"

    def test_merge_tie_broken_by_node_id(self):
        a = LWWRegister(value="from_a", timestamp=5.0, node_id="aaa")
        b = LWWRegister(value="from_b", timestamp=5.0, node_id="zzz")
        merged = a.merge(b)
        assert merged.value == "from_b"  # "zzz" > "aaa"

    def test_to_dict_from_dict_roundtrip(self):
        reg = LWWRegister(value=42, timestamp=1000.0, node_id="n5")
        d = reg.to_dict()
        restored = LWWRegister.from_dict(d)
        assert restored.value == 42
        assert restored.timestamp == 1000.0
        assert restored.node_id == "n5"

    def test_from_dict_defaults(self):
        reg = LWWRegister.from_dict({})
        assert reg.value is None
        assert reg.timestamp == 0.0
        assert reg.node_id == ""


# ── AppBudgetCRDT ────────────────────────────────────────────────────────────

class TestAppBudgetCRDT:
    def test_is_over_budget_no_limit(self):
        ab = AppBudgetCRDT(app_id="app1")
        assert ab.is_over_budget() is False

    def test_is_over_budget_zero_limit(self):
        ab = AppBudgetCRDT(app_id="app1")
        ab.budget_limit_cents.set(0, "hub")
        assert ab.is_over_budget() is False

    def test_is_over_budget_true(self):
        ab = AppBudgetCRDT(app_id="app1")
        ab.budget_limit_cents.set(100, "hub")
        ab.spend_cents.increment("n1", 100)
        assert ab.is_over_budget() is True

    def test_remaining_cents_unlimited(self):
        ab = AppBudgetCRDT(app_id="app1")
        assert ab.remaining_cents() == 999999999

    def test_remaining_cents_with_limit(self):
        ab = AppBudgetCRDT(app_id="app1")
        ab.budget_limit_cents.set(500, "hub")
        ab.spend_cents.increment("n1", 200)
        assert ab.remaining_cents() == 300

    def test_remaining_cents_over_budget_returns_zero(self):
        ab = AppBudgetCRDT(app_id="app1")
        ab.budget_limit_cents.set(100, "hub")
        ab.spend_cents.increment("n1", 200)
        assert ab.remaining_cents() == 0

    def test_merge(self):
        a = AppBudgetCRDT(app_id="app1")
        a.spend_cents.increment("n1", 50)
        a.call_count.increment("n1", 3)

        b = AppBudgetCRDT(app_id="app1")
        b.spend_cents.increment("n2", 30)
        b.call_count.increment("n2", 2)

        merged = a.merge(b)
        assert merged.spend_cents.value == 80
        assert merged.call_count.value == 5
        assert merged.app_id == "app1"

    def test_to_dict_from_dict_roundtrip(self):
        ab = AppBudgetCRDT(app_id="test-app")
        ab.spend_cents.increment("n1", 42)
        ab.budget_limit_cents.set(1000, "hub")

        d = ab.to_dict()
        restored = AppBudgetCRDT.from_dict(d)
        assert restored.app_id == "test-app"
        assert restored.spend_cents.value == 42
        assert restored.budget_limit_cents.value == 1000


# ── Delta Encoding ───────────────────────────────────────────────────────────

class TestDeltaEncoding:
    def test_encode_decode_roundtrip(self):
        state = AppBudgetCRDT(app_id="myapp")
        state.spend_cents.increment("n1", 100)
        state.call_count.increment("n1", 5)

        packet = encode_delta("node-abc", {"myapp": state})
        node_id, states = decode_delta(packet)

        assert node_id == "node-abc"
        assert "myapp" in states
        assert states["myapp"].spend_cents.value == 100
        assert states["myapp"].call_count.value == 5

    def test_encode_decode_multiple_apps(self):
        s1 = AppBudgetCRDT(app_id="a1")
        s1.spend_cents.increment("n", 10)
        s2 = AppBudgetCRDT(app_id="a2")
        s2.spend_cents.increment("n", 20)

        packet = encode_delta("n", {"a1": s1, "a2": s2})
        node_id, states = decode_delta(packet)
        assert len(states) == 2
        assert states["a1"].spend_cents.value == 10
        assert states["a2"].spend_cents.value == 20

    def test_decode_invalid_magic(self):
        with pytest.raises(ValueError, match="Invalid CRDT packet magic"):
            decode_delta(b"BADM" + b"\x00" * 20)

    def test_decode_wrong_version(self):
        data = CRDT_MAGIC + struct.pack("!B", 99)
        with pytest.raises(ValueError, match="Unsupported CRDT version"):
            decode_delta(data + b"\x00" * 20)

    def test_encode_empty_states(self):
        packet = encode_delta("n", {})
        node_id, states = decode_delta(packet)
        assert node_id == "n"
        assert len(states) == 0


# ── CRDTSyncNode ────────────────────────────────────────────────────────────

class TestCRDTSyncNode:
    def test_record_spend_creates_state(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        result = node.record_spend("app1", 50)
        assert result is True
        state = node.get_state("app1")
        assert state is not None
        assert state.spend_cents.value == 50

    def test_record_spend_over_budget(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("app1", 100)
        node.record_spend("app1", 100)
        # Should now be at budget
        result = node.record_spend("app1", 10)
        assert result is False

    def test_check_budget_no_state(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        allowed, remaining = node.check_budget("unknown")
        assert allowed is True
        assert remaining == 999999999

    def test_check_budget_within_limit(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("app1", 500)
        node.record_spend("app1", 200)
        allowed, remaining = node.check_budget("app1")
        assert allowed is True
        assert remaining == 300

    def test_set_budget_limit_creates_state(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("new-app", 1000)
        state = node.get_state("new-app")
        assert state is not None
        assert state.budget_limit_cents.value == 1000

    def test_get_all_states(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.record_spend("a", 10)
        node.record_spend("b", 20)
        states = node.get_all_states()
        assert "a" in states
        assert "b" in states

    def test_get_stats(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.record_spend("a", 10)
        stats = node.get_stats()
        assert stats["node_id"] == "test-node"
        assert stats["tracked_apps"] == 1
        assert stats["local_checks"] == 1

    def test_node_id_property(self):
        node = CRDTSyncNode(node_id="abc123", gossip_port=0)
        assert node.node_id == "abc123"

    def test_is_active_default_false(self):
        node = CRDTSyncNode(node_id="test", gossip_port=0)
        assert node.is_active is False

    def test_generate_node_id(self):
        nid = CRDTSyncNode._generate_node_id()
        assert len(nid) == 12
        assert isinstance(nid, str)

    def test_stop_without_start(self):
        node = CRDTSyncNode(node_id="test", gossip_port=0)
        node.stop()  # should not raise
        assert node.is_active is False

    def test_handle_incoming_delta(self):
        node = CRDTSyncNode(node_id="receiver", gossip_port=0)
        # Create a valid delta packet
        remote_state = AppBudgetCRDT(app_id="app1")
        remote_state.spend_cents.increment("sender", 100)
        packet = encode_delta("sender", {"app1": remote_state})
        node._handle_incoming_delta(packet, ("127.0.0.1", 9999))
        state = node.get_state("app1")
        assert state is not None
        assert state.spend_cents.value == 100

    def test_handle_incoming_delta_merges(self):
        node = CRDTSyncNode(node_id="receiver", gossip_port=0)
        node.record_spend("app1", 50)
        remote_state = AppBudgetCRDT(app_id="app1")
        remote_state.spend_cents.increment("sender", 100)
        packet = encode_delta("sender", {"app1": remote_state})
        node._handle_incoming_delta(packet, ("127.0.0.1", 9999))
        state = node.get_state("app1")
        assert state.spend_cents.value == 150  # 50 + 100

    def test_handle_incoming_delta_invalid_data(self):
        node = CRDTSyncNode(node_id="test", gossip_port=0)
        # Should not raise, just log warning
        node._handle_incoming_delta(b"garbage data", ("127.0.0.1", 9999))

    def test_send_deltas_no_dirty(self):
        node = CRDTSyncNode(node_id="test", gossip_port=0)
        # No dirty apps, should return without error
        node._send_deltas()


# ── CRDTHub ──────────────────────────────────────────────────────────────────

class TestCRDTHub:
    def test_set_budget(self):
        hub = CRDTHub(listen_port=0)
        hub.set_budget("app1", 5000)
        state = hub.get_app_state("app1")
        assert state is not None
        assert state.budget_limit_cents.value == 5000

    def test_record_spend(self):
        hub = CRDTHub(listen_port=0)
        total = hub.record_spend("app1", 100)
        assert total == 100
        total = hub.record_spend("app1", 50)
        assert total == 150

    def test_get_global_state(self):
        hub = CRDTHub(listen_port=0)
        hub.record_spend("a", 10)
        hub.record_spend("b", 20)
        gs = hub.get_global_state()
        assert "a" in gs
        assert "b" in gs

    def test_get_app_state_missing(self):
        hub = CRDTHub(listen_port=0)
        assert hub.get_app_state("nonexistent") is None

    def test_is_active_default_false(self):
        hub = CRDTHub(listen_port=0)
        assert hub.is_active is False

    def test_listen_port_property(self):
        hub = CRDTHub(listen_port=9999)
        assert hub.listen_port == 9999
        hub.listen_port = 8888
        assert hub.listen_port == 8888

    def test_persist_interval_property(self):
        hub = CRDTHub(listen_port=0, persist_interval_seconds=30.0)
        assert hub.persist_interval == 30.0
        hub.persist_interval = 60.0
        assert hub.persist_interval == 60.0

    def test_get_stats(self):
        hub = CRDTHub(listen_port=0)
        stats = hub.get_stats()
        assert "active" in stats
        assert "listen_port" in stats
        assert stats["tracked_apps"] == 0

    def test_stop_without_start(self):
        hub = CRDTHub(listen_port=0)
        hub.stop()  # should not raise

    def test_set_budget_merge_with_existing(self):
        hub = CRDTHub(listen_port=0)
        hub.record_spend("app1", 200)
        hub.set_budget("app1", 1000)
        state = hub.get_app_state("app1")
        assert state.budget_limit_cents.value == 1000
        assert state.spend_cents.value == 200

    def test_broadcast_state_no_global_state(self):
        hub = CRDTHub(listen_port=0)
        # Should not raise with empty global state
        hub._broadcast_state()

    def test_persist_to_db_empty(self):
        hub = CRDTHub(listen_port=0)
        # Should not raise with no data
        hub._persist_to_db()

    @patch("orchestrator.core.crdt_sync.Path")
    def test_persist_to_db_with_data(self, mock_path_cls):
        hub = CRDTHub(listen_port=0)
        hub.record_spend("app1", 100)
        mock_file = MagicMock()
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_file)
        mock_file.parent = MagicMock()
        # Just verify it doesn't raise
        hub._persist_to_db()


# ── Module singletons ────────────────────────────────────────────────────────

class TestSingletons:
    def test_merge_strategy_enum(self):
        assert MergeStrategy.MAX == "max"
        assert MergeStrategy.SUM == "sum"
        assert MergeStrategy.LWW == "lww"
