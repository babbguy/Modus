"""
CRDT sync primitives + maintenance module helpers.

Targets:
  - orchestrator.core.crdt_sync (GCounter, PNCounter, LWWRegister,
    AppBudgetCRDT, encode_delta, decode_delta)
  - orchestrator.core.maintenance (ensure_partitions no-op, prune
    function signatures, compact helpers)
"""
from __future__ import annotations


import pytest
# ═══════════════════════════════════════════════════════════════════════════════
# 1. GCounter
# ═══════════════════════════════════════════════════════════════════════════════

def test_gcounter_basic():
    from orchestrator.core.crdt_sync import GCounter
    c = GCounter()
    c.increment("node-a", 5)
    c.increment("node-b", 3)
    assert c.value == 8


def test_gcounter_merge():
    from orchestrator.core.crdt_sync import GCounter
    c1 = GCounter()
    c1.increment("node-a", 5)
    c1.increment("node-b", 3)

    c2 = GCounter()
    c2.increment("node-a", 3)  # less than c1
    c2.increment("node-b", 7)  # more than c1
    c2.increment("node-c", 2)  # only in c2

    merged = c1.merge(c2)
    assert merged.counts["node-a"] == 5  # max(5, 3)
    assert merged.counts["node-b"] == 7  # max(3, 7)
    assert merged.counts["node-c"] == 2
    assert merged.value == 14


def test_gcounter_serialization():
    from orchestrator.core.crdt_sync import GCounter
    c = GCounter()
    c.increment("n1", 10)
    d = c.to_dict()
    assert d == {"n1": 10}

    restored = GCounter.from_dict(d)
    assert restored.value == 10


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PNCounter
# ═══════════════════════════════════════════════════════════════════════════════

def test_pncounter_basic():
    from orchestrator.core.crdt_sync import PNCounter
    c = PNCounter()
    c.increment("node-a", 100)
    c.decrement("node-a", 20)
    assert c.value == 80


def test_pncounter_merge():
    from orchestrator.core.crdt_sync import PNCounter
    c1 = PNCounter()
    c1.increment("a", 100)
    c1.decrement("a", 10)

    c2 = PNCounter()
    c2.increment("a", 80)
    c2.decrement("a", 30)

    merged = c1.merge(c2)
    assert merged.p.counts["a"] == 100  # max(100, 80)
    assert merged.n.counts["a"] == 30   # max(10, 30)
    assert merged.value == 70


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

def test_lww_register_basic():
    from orchestrator.core.crdt_sync import LWWRegister
    r = LWWRegister()
    r.set("policy-v1", "node-a")
    assert r.value == "policy-v1"
    assert r.node_id == "node-a"


def test_lww_register_merge_higher_timestamp():
    from orchestrator.core.crdt_sync import LWWRegister
    r1 = LWWRegister(value="old", timestamp=1000.0, node_id="a")
    r2 = LWWRegister(value="new", timestamp=2000.0, node_id="b")
    merged = r1.merge(r2)
    assert merged.value == "new"


def test_lww_register_merge_lower_timestamp():
    from orchestrator.core.crdt_sync import LWWRegister
    r1 = LWWRegister(value="current", timestamp=2000.0, node_id="a")
    r2 = LWWRegister(value="old", timestamp=1000.0, node_id="b")
    merged = r1.merge(r2)
    assert merged.value == "current"


def test_lww_register_merge_tie_break():
    from orchestrator.core.crdt_sync import LWWRegister
    r1 = LWWRegister(value="from-a", timestamp=1000.0, node_id="a")
    r2 = LWWRegister(value="from-b", timestamp=1000.0, node_id="b")
    merged = r1.merge(r2)
    assert merged.value == "from-b"  # "b" > "a" for tie-break


def test_lww_register_serialization():
    from orchestrator.core.crdt_sync import LWWRegister
    r = LWWRegister(value="test", timestamp=1234.5, node_id="n1")
    d = r.to_dict()
    restored = LWWRegister.from_dict(d)
    assert restored.value == "test"
    assert restored.timestamp == 1234.5
    assert restored.node_id == "n1"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. AppBudgetCRDT
# ═══════════════════════════════════════════════════════════════════════════════

def test_app_budget_crdt_not_over_budget():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app-1")
    state.budget_limit_cents.set(10000, "orch")  # $100 budget
    state.spend_cents.increment("agent-1", 5000)  # $50 spent
    assert state.is_over_budget() is False
    assert state.remaining_cents() == 5000


def test_app_budget_crdt_over_budget():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app-1")
    state.budget_limit_cents.set(10000, "orch")
    state.spend_cents.increment("agent-1", 12000)
    assert state.is_over_budget() is True
    assert state.remaining_cents() == 0


def test_app_budget_crdt_no_limit():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app-1")
    state.spend_cents.increment("agent-1", 999999)
    assert state.is_over_budget() is False
    assert state.remaining_cents() == 999999999


def test_app_budget_crdt_merge():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    s1 = AppBudgetCRDT(app_id="app-1")
    s1.spend_cents.increment("agent-1", 100)
    s1.call_count.increment("agent-1", 10)

    s2 = AppBudgetCRDT(app_id="app-1")
    s2.spend_cents.increment("agent-2", 200)
    s2.call_count.increment("agent-2", 20)

    merged = s1.merge(s2)
    assert merged.spend_cents.value == 300
    assert merged.call_count.value == 30


def test_app_budget_crdt_serialization():
    from orchestrator.core.crdt_sync import AppBudgetCRDT
    state = AppBudgetCRDT(app_id="app-1")
    state.spend_cents.increment("a", 100)
    state.call_count.increment("a", 5)
    state.budget_limit_cents.set(10000, "orch")

    d = state.to_dict()
    restored = AppBudgetCRDT.from_dict(d)
    assert restored.app_id == "app-1"
    assert restored.spend_cents.value == 100
    assert restored.call_count.value == 5


# ═══════════════════════════════════════════════════════════════════════════════
# 5. DELTA ENCODING / DECODING
# ═══════════════════════════════════════════════════════════════════════════════

def test_encode_decode_delta():
    from orchestrator.core.crdt_sync import (
        AppBudgetCRDT, encode_delta, decode_delta,
    )
    state = AppBudgetCRDT(app_id="app-123")
    state.spend_cents.increment("node-1", 500)
    state.call_count.increment("node-1", 10)
    state.budget_limit_cents.set(50000, "orch")

    states = {"app-123": state}
    encoded = encode_delta("node-1", states)

    assert isinstance(encoded, bytes)
    assert encoded[:4] == b"CNTL"

    node_id, decoded_states = decode_delta(encoded)
    assert node_id == "node-1"
    assert "app-123" in decoded_states
    assert decoded_states["app-123"].spend_cents.value == 500


def test_encode_decode_multiple_apps():
    from orchestrator.core.crdt_sync import (
        AppBudgetCRDT, encode_delta, decode_delta,
    )
    s1 = AppBudgetCRDT(app_id="app-1")
    s1.spend_cents.increment("n1", 100)
    s2 = AppBudgetCRDT(app_id="app-2")
    s2.spend_cents.increment("n1", 200)

    encoded = encode_delta("n1", {"app-1": s1, "app-2": s2})
    node_id, decoded = decode_delta(encoded)
    assert len(decoded) == 2
    assert decoded["app-1"].spend_cents.value == 100
    assert decoded["app-2"].spend_cents.value == 200


def test_decode_bad_magic():
    from orchestrator.core.crdt_sync import decode_delta
    with pytest.raises(ValueError, match="magic"):
        decode_delta(b"BAAD" + b"\x00" * 20)


def test_decode_bad_version():
    import struct
    from orchestrator.core.crdt_sync import decode_delta, CRDT_MAGIC
    data = CRDT_MAGIC + struct.pack("!B", 99) + b"\x00" * 20
    with pytest.raises(ValueError, match="version"):
        decode_delta(data)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. MERGE STRATEGY ENUM
# ═══════════════════════════════════════════════════════════════════════════════

def test_merge_strategy_enum():
    from orchestrator.core.crdt_sync import MergeStrategy
    assert MergeStrategy.MAX == "max"
    assert MergeStrategy.SUM == "sum"
    assert MergeStrategy.LWW == "lww"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. MAINTENANCE — ensure_partitions (no-op on SQLite)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_ensure_partitions_noop():
    """ensure_partitions should be a no-op on SQLite."""
    from orchestrator.core.maintenance import ensure_partitions
    await ensure_partitions()  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# 8. MAINTENANCE — ensure_partitions is no-op on SQLite (covers the guard)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_ensure_partitions_sqlite():
    """ensure_partitions is a no-op on SQLite (guards check is_sqlite)."""
    from orchestrator.core.maintenance import ensure_partitions
    from orchestrator.core.config import settings
    assert settings.is_sqlite  # tests run on SQLite
    await ensure_partitions()  # should return immediately


async def test_prune_real_time_spend_no_factory():
    """prune_real_time_spend should return 0 when no factory available."""
    from orchestrator.core.maintenance import prune_real_time_spend
    # In test context with the global factory potentially set, this will
    # either run against the in-memory DB (finding nothing to prune) or
    # return immediately if factory is None.
    await prune_real_time_spend()
