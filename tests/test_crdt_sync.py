"""
Tests for orchestrator.core.crdt_sync — CRDT local-first sync engine.

Tests CRDT primitives (GCounter, PNCounter, LWWRegister), app budget CRDT,
delta encoding/decoding, CRDTSyncNode local enforcement, and CRDTHub merge.
"""

from __future__ import annotations


import pytest

from orchestrator.core.crdt_sync import (
    GCounter,
    PNCounter,
    LWWRegister,
    AppBudgetCRDT,
    CRDTSyncNode,
    CRDTHub,
    encode_delta,
    decode_delta,
    CRDT_MAGIC,
)


# ── GCounter ────────────────────────────────────────────────────────────────


class TestGCounter:
    def test_empty_value(self):
        gc = GCounter()
        assert gc.value == 0

    def test_increment_single_node(self):
        gc = GCounter()
        gc.increment("node1", 5)
        assert gc.value == 5

    def test_increment_multiple_nodes(self):
        gc = GCounter()
        gc.increment("node1", 3)
        gc.increment("node2", 7)
        assert gc.value == 10

    def test_merge_disjoint(self):
        gc1 = GCounter(counts={"a": 5})
        gc2 = GCounter(counts={"b": 3})
        merged = gc1.merge(gc2)
        assert merged.value == 8
        assert merged.counts == {"a": 5, "b": 3}

    def test_merge_overlapping_takes_max(self):
        gc1 = GCounter(counts={"a": 5, "b": 2})
        gc2 = GCounter(counts={"a": 3, "b": 7})
        merged = gc1.merge(gc2)
        assert merged.counts["a"] == 5  # max(5, 3)
        assert merged.counts["b"] == 7  # max(2, 7)
        assert merged.value == 12

    def test_merge_is_commutative(self):
        gc1 = GCounter(counts={"a": 5, "c": 1})
        gc2 = GCounter(counts={"a": 3, "b": 7})
        assert gc1.merge(gc2).value == gc2.merge(gc1).value

    def test_merge_is_idempotent(self):
        gc = GCounter(counts={"a": 5})
        merged = gc.merge(gc)
        assert merged.value == gc.value

    def test_to_from_dict_roundtrip(self):
        gc = GCounter(counts={"x": 10, "y": 20})
        restored = GCounter.from_dict(gc.to_dict())
        assert restored.counts == gc.counts


# ── PNCounter ───────────────────────────────────────────────────────────────


class TestPNCounter:
    def test_empty_value(self):
        pn = PNCounter()
        assert pn.value == 0

    def test_increment_only(self):
        pn = PNCounter()
        pn.increment("node1", 100)
        assert pn.value == 100

    def test_decrement(self):
        pn = PNCounter()
        pn.increment("node1", 100)
        pn.decrement("node1", 30)
        assert pn.value == 70

    def test_merge(self):
        pn1 = PNCounter()
        pn1.increment("node1", 50)
        pn1.decrement("node1", 10)

        pn2 = PNCounter()
        pn2.increment("node2", 30)

        merged = pn1.merge(pn2)
        assert merged.value == 70  # (50+30) - 10

    def test_to_from_dict_roundtrip(self):
        pn = PNCounter()
        pn.increment("a", 10)
        pn.decrement("b", 3)
        restored = PNCounter.from_dict(pn.to_dict())
        assert restored.value == pn.value


# ── LWWRegister ─────────────────────────────────────────────────────────────


class TestLWWRegister:
    def test_empty(self):
        r = LWWRegister()
        assert r.value is None
        assert r.timestamp == 0.0

    def test_set(self):
        r = LWWRegister()
        r.set(42, "node1")
        assert r.value == 42

    def test_merge_higher_timestamp_wins(self):
        r1 = LWWRegister(value=1, timestamp=10.0, node_id="a")
        r2 = LWWRegister(value=2, timestamp=20.0, node_id="b")
        merged = r1.merge(r2)
        assert merged.value == 2

    def test_merge_equal_timestamp_higher_node_wins(self):
        r1 = LWWRegister(value=1, timestamp=10.0, node_id="a")
        r2 = LWWRegister(value=2, timestamp=10.0, node_id="b")
        merged = r1.merge(r2)
        assert merged.value == 2  # "b" > "a"

    def test_to_from_dict(self):
        r = LWWRegister(value="hello", timestamp=100.0, node_id="n1")
        restored = LWWRegister.from_dict(r.to_dict())
        assert restored.value == "hello"
        assert restored.timestamp == 100.0


# ── AppBudgetCRDT ───────────────────────────────────────────────────────────


class TestAppBudgetCRDT:
    def test_initial_not_over_budget(self):
        state = AppBudgetCRDT(app_id="app1")
        assert not state.is_over_budget()

    def test_over_budget_detection(self):
        state = AppBudgetCRDT(app_id="app1")
        state.budget_limit_cents.set(100, "hub")
        state.spend_cents.increment("node1", 150)
        assert state.is_over_budget()

    def test_remaining_cents(self):
        state = AppBudgetCRDT(app_id="app1")
        state.budget_limit_cents.set(1000, "hub")
        state.spend_cents.increment("node1", 300)
        assert state.remaining_cents() == 700

    def test_remaining_cents_no_limit(self):
        state = AppBudgetCRDT(app_id="app1")
        assert state.remaining_cents() == 999999999

    def test_merge_two_nodes(self):
        s1 = AppBudgetCRDT(app_id="app1")
        s1.spend_cents.increment("node1", 50)
        s1.call_count.increment("node1", 3)

        s2 = AppBudgetCRDT(app_id="app1")
        s2.spend_cents.increment("node2", 30)
        s2.call_count.increment("node2", 2)

        merged = s1.merge(s2)
        assert merged.spend_cents.value == 80
        assert merged.call_count.value == 5

    def test_to_from_dict_roundtrip(self):
        state = AppBudgetCRDT(app_id="test")
        state.spend_cents.increment("n1", 42)
        state.budget_limit_cents.set(1000, "hub")

        restored = AppBudgetCRDT.from_dict(state.to_dict())
        assert restored.app_id == "test"
        assert restored.spend_cents.value == 42
        assert restored.budget_limit_cents.value == 1000


# ── Delta encoding ──────────────────────────────────────────────────────────


class TestDeltaEncoding:
    def test_encode_decode_roundtrip(self):
        state = AppBudgetCRDT(app_id="app1")
        state.spend_cents.increment("node1", 100)
        state.call_count.increment("node1", 5)
        state.budget_limit_cents.set(5000, "hub")

        packet = encode_delta("node1", {"app1": state})
        assert packet[:4] == CRDT_MAGIC

        node_id, states = decode_delta(packet)
        assert node_id == "node1"
        assert "app1" in states
        assert states["app1"].spend_cents.value == 100
        assert states["app1"].call_count.value == 5

    def test_encode_decode_multiple_apps(self):
        states = {}
        for i in range(5):
            s = AppBudgetCRDT(app_id=f"app{i}")
            s.spend_cents.increment("n1", i * 10)
            states[f"app{i}"] = s

        packet = encode_delta("n1", states)
        node_id, decoded = decode_delta(packet)
        assert len(decoded) == 5
        assert decoded["app3"].spend_cents.value == 30

    def test_invalid_magic_raises(self):
        with pytest.raises(ValueError, match="magic"):
            decode_delta(b"XXXX" + b"\x00" * 20)

    def test_invalid_version_raises(self):
        bad = CRDT_MAGIC + b"\xFF" + b"\x00" * 20
        with pytest.raises(ValueError, match="version"):
            decode_delta(bad)


# ── CRDTSyncNode ───────────────────────────────────────────────────────────


class TestCRDTSyncNode:
    def test_record_spend_creates_state(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        result = node.record_spend("app1", 50)
        assert result is True  # within budget (no limit set)

        state = node.get_state("app1")
        assert state is not None
        assert state.spend_cents.value == 50

    def test_budget_enforcement(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("app1", 100)

        # Spend within budget
        assert node.record_spend("app1", 50) is True
        assert node.record_spend("app1", 40) is True

        # Next spend would exceed
        assert node.record_spend("app1", 20) is True  # 50+40=90, still under 100

        # Now over budget
        assert node.record_spend("app1", 20) is False  # 90+20=110, over 100

    def test_check_budget(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.set_budget_limit("app1", 1000)

        allowed, remaining = node.check_budget("app1")
        assert allowed is True
        assert remaining == 1000

    def test_check_budget_unknown_app(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        allowed, remaining = node.check_budget("unknown")
        assert allowed is True
        assert remaining == 999999999

    def test_stats(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.record_spend("app1", 10)
        node.record_spend("app1", 20)

        stats = node.get_stats()
        assert stats["node_id"] == "test-node"
        assert stats["local_checks"] == 2
        assert stats["tracked_apps"] == 1

    def test_dirty_tracking(self):
        node = CRDTSyncNode(node_id="test-node", gossip_port=0)
        node.record_spend("app1", 10)
        node.record_spend("app2", 20)

        assert "app1" in node._dirty
        assert "app2" in node._dirty


# ── CRDTHub ─────────────────────────────────────────────────────────────────


class TestCRDTHub:
    def test_initial_empty(self):
        hub = CRDTHub(listen_port=0)
        assert len(hub.get_global_state()) == 0

    def test_manual_state_injection(self):
        hub = CRDTHub(listen_port=0)
        state = AppBudgetCRDT(app_id="app1")
        state.spend_cents.increment("node1", 100)

        with hub._lock:
            hub._global_state["app1"] = state

        retrieved = hub.get_app_state("app1")
        assert retrieved is not None
        assert retrieved.spend_cents.value == 100

    def test_get_stats(self):
        hub = CRDTHub(listen_port=0)
        stats = hub.get_stats()
        assert stats["active"] is False
        assert stats["tracked_apps"] == 0
