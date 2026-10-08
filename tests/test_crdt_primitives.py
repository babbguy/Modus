"""
CRDT Sync coverage.

Targets orchestrator.core.crdt_sync: GCounter, PNCounter, LWWRegister,
AppBudgetCRDT, delta encode/decode, CRDTSyncNode operations.
"""
from __future__ import annotations


import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# 1. GCounter
# ═══════════════════════════════════════════════════════════════════════════════

def test_gcounter_increment():
    from orchestrator.core.crdt_sync import GCounter
    c = GCounter()
    c.increment("node1", 5)
    c.increment("node1", 3)
    assert c.value == 8


def test_gcounter_multiple_nodes():
    from orchestrator.core.crdt_sync import GCounter
    c = GCounter()
    c.increment("node1", 10)
    c.increment("node2", 20)
    assert c.value == 30


def test_gcounter_merge():
    from orchestrator.core.crdt_sync import GCounter
    a = GCounter()
    a.increment("node1", 10)
    a.increment("node2", 5)
    b = GCounter()
    b.increment("node1", 8)
    b.increment("node2", 15)
    b.increment("node3", 3)
    merged = a.merge(b)
    assert merged.counts["node1"] == 10  # max(10, 8)
    assert merged.counts["node2"] == 15  # max(5, 15)
    assert merged.counts["node3"] == 3
    assert merged.value == 28


def test_gcounter_serialization():
    from orchestrator.core.crdt_sync import GCounter
    c = GCounter()
    c.increment("n1", 5)
    d = c.to_dict()
    restored = GCounter.from_dict(d)
    assert restored.value == 5
    assert restored.counts["n1"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PNCounter
# ═══════════════════════════════════════════════════════════════════════════════

def test_pncounter_increment_decrement():
    from orchestrator.core.crdt_sync import PNCounter
    c = PNCounter()
    c.increment("node1", 100)
    c.decrement("node1", 30)
    assert c.value == 70


def test_pncounter_merge():
    from orchestrator.core.crdt_sync import PNCounter
    a = PNCounter()
    a.increment("node1", 100)
    a.decrement("node1", 10)
    b = PNCounter()
    b.increment("node1", 80)
    b.decrement("node1", 20)
    merged = a.merge(b)
    # P: max(100, 80) = 100, N: max(10, 20) = 20
    assert merged.value == 80


def test_pncounter_serialization():
    from orchestrator.core.crdt_sync import PNCounter
    c = PNCounter()
    c.increment("n1", 50)
    c.decrement("n1", 10)
    d = c.to_dict()
    restored = PNCounter.from_dict(d)
    assert restored.value == 40


# ═══════════════════════════════════════════════════════════════════════════════
# 3. LWWRegister
# ═══════════════════════════════════════════════════════════════════════════════

def test_lww_register_set():
    from orchestrator.core.crdt_sync import LWWRegister
    r = LWWRegister()
    r.set(100, "node1")
    assert r.value == 100
    assert r.node_id == "node1"


def test_lww_register_merge_higher_ts():
    from orchestrator.core.crdt_sync import LWWRegister
    a = LWWRegister(value=100, timestamp=1.0, node_id="a")
    b = LWWRegister(value=200, timestamp=2.0, node_id="b")
    merged = a.merge(b)
    assert merged.value == 200  # b wins (higher timestamp)


def test_lww_register_merge_same_ts_tiebreak():
    from orchestrator.core.crdt_sync import LWWRegister
    a = LWWRegister(value=100, timestamp=1.0, node_id="a")
    b = LWWRegister(value=200, timestamp=1.0, node_id="b")
    merged = a.merge(b)
    assert merged.value == 200  # b wins (higher node_id)


def test_lww_register_merge_lower_ts():
    from orchestrator.core.crdt_sync import LWWRegister
    a = LWWRegister(value=100, timestamp=2.0, node_id="a")
    b = LWWRegister(value=200, timestamp=1.0, node_id="b")
    merged = a.merge(b)
    assert merged.value == 100  # a wins (higher timestamp)


def test_lww_register_serialization():
    from orchestrator.core.crdt_sync import LWWRegister
    r = LWWRegister(value="policy-v2", timestamp=1234.5, node_id="n1")
    d = r.to_dict()
    restored = LWWRegister.from_dict(d)
    assert restored.value == "policy-v2"
    assert restored.timestamp == 1234.5


# ═══════════════════════════════════════════════════════════════════════════════
# 4. AppBudgetCRDT
# ═══════════════════════════════════════════════════════════════════════════════

def test_app_budget_crdt_over_budget():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app1")
    state.budget_limit_cents.set(100, "orchestrator")
    state.spend_cents.increment("agent1", 150)
    assert state.is_over_budget() is True
    assert state.remaining_cents() == 0


def test_app_budget_crdt_within_budget():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app1")
    state.budget_limit_cents.set(1000, "orchestrator")
    state.spend_cents.increment("agent1", 500)
    assert state.is_over_budget() is False
    assert state.remaining_cents() == 500


def test_app_budget_crdt_no_limit():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app1")
    assert state.is_over_budget() is False
    assert state.remaining_cents() == 999999999


def test_app_budget_crdt_merge():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    a = AppBudgetCRDT(app_id="app1")
    a.spend_cents.increment("agent1", 100)
    a.call_count.increment("agent1", 5)
    b = AppBudgetCRDT(app_id="app1")
    b.spend_cents.increment("agent2", 200)
    b.call_count.increment("agent2", 10)
    merged = a.merge(b)
    assert merged.spend_cents.value == 300
    assert merged.call_count.value == 15


def test_app_budget_crdt_serialization():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app1")
    state.spend_cents.increment("n1", 50)
    state.budget_limit_cents.set(1000, "orch")
    d = state.to_dict()
    restored = AppBudgetCRDT.from_dict(d)
    assert restored.app_id == "app1"
    assert restored.spend_cents.value == 50


# ═══════════════════════════════════════════════════════════════════════════════
# 5. DELTA ENCODE / DECODE
# ═══════════════════════════════════════════════════════════════════════════════

def test_encode_decode_roundtrip():
    from orchestrator.core.crdt_sync import encode_delta, decode_delta, AppBudgetCRDT
    states = {
        "app1": AppBudgetCRDT(app_id="app1"),
        "app2": AppBudgetCRDT(app_id="app2"),
    }
    states["app1"].spend_cents.increment("node1", 100)
    states["app2"].call_count.increment("node1", 5)

    data = encode_delta("node1", states)
    node_id, decoded = decode_delta(data)
    assert node_id == "node1"
    assert "app1" in decoded
    assert "app2" in decoded
    assert decoded["app1"].spend_cents.value == 100
    assert decoded["app2"].call_count.value == 5


def test_decode_invalid_magic():
    from orchestrator.core.crdt_sync import decode_delta
    with pytest.raises(ValueError, match="magic"):
        decode_delta(b"BADMxxxxxxx")


def test_decode_invalid_version():
    from orchestrator.core.crdt_sync import decode_delta, CRDT_MAGIC
    import struct
    data = CRDT_MAGIC + struct.pack("!B", 99) + b"\x00\x00"
    with pytest.raises(ValueError, match="version"):
        decode_delta(data)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CRDTSyncNode
# ═══════════════════════════════════════════════════════════════════════════════

def test_sync_node_record_spend():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    node = CRDTSyncNode(node_id="test-node")
    assert node.record_spend("app1", 50) is True
    state = node.get_state("app1")
    assert state.spend_cents.value == 50


def test_sync_node_budget_denial():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    node = CRDTSyncNode(node_id="test-node")
    node.set_budget_limit("app1", 100)
    assert node.record_spend("app1", 50) is True
    assert node.record_spend("app1", 60) is True  # total 110
    # Now over budget
    assert node.record_spend("app1", 1) is False


def test_sync_node_check_budget():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    node = CRDTSyncNode(node_id="test-node")
    allowed, remaining = node.check_budget("unknown-app")
    assert allowed is True
    assert remaining == 999999999

    node.set_budget_limit("app1", 1000)
    node.record_spend("app1", 300)
    allowed, remaining = node.check_budget("app1")
    assert allowed is True
    assert remaining == 700


def test_sync_node_get_all_states():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    node = CRDTSyncNode(node_id="test-node")
    node.record_spend("app1", 10)
    node.record_spend("app2", 20)
    states = node.get_all_states()
    assert "app1" in states
    assert "app2" in states


def test_sync_node_get_stats():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    node = CRDTSyncNode(node_id="test-node")
    stats = node.get_stats()
    assert "local_checks" in stats
    assert "gossip_sent" in stats
    assert "budget_denials" in stats


def test_sync_node_set_budget_limit():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    node = CRDTSyncNode(node_id="local-node")
    node.set_budget_limit("app1", 5000)
    state = node.get_state("app1")
    assert state.budget_limit_cents.value == 5000
    assert state.remaining_cents() == 5000

    # Record some spend
    node.record_spend("app1", 1000)
    state = node.get_state("app1")
    assert state.remaining_cents() == 4000


def test_sync_node_stop_without_start():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    node = CRDTSyncNode(node_id="test-node")
    node.stop()  # Should not raise


def test_sync_node_generate_node_id():
    from orchestrator.core.crdt_sync import CRDTSyncNode
    nid = CRDTSyncNode._generate_node_id()
    assert len(nid) == 12
    assert isinstance(nid, str)
