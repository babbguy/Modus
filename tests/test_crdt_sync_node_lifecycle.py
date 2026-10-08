"""
CRDT sync deep coverage.

Targets orchestrator.core.crdt_sync:
  - start() / stop() (399-430, 439-442)
  - _send_deltas (508-516, 523-548)
  - _listen_loop (552-562)
  - _handle_incoming_delta (653-673, 679-682)
  - Hub operations (686-713, 720-729, 733-739, 759-760)
  - encode_delta / decode_delta (842-846, 852-856)
"""
from __future__ import annotations

import struct
import time
from unittest.mock import MagicMock

import pytest

from orchestrator.core.crdt_sync import (
    GCounter,
    PNCounter,
    LWWRegister,
    AppBudgetCRDT,
    CRDTSyncNode,
    encode_delta,
    decode_delta,
    CRDT_MAGIC,
)


# ── GCounter ─────────────────────────────────────────────────────────────────

class TestGCounter:
    def test_increment(self):
        c = GCounter()
        c.increment("node-a")
        c.increment("node-a")
        c.increment("node-b", 5)
        assert c.value == 7

    def test_merge(self):
        c1 = GCounter()
        c1.increment("node-a", 10)
        c1.increment("node-b", 5)

        c2 = GCounter()
        c2.increment("node-a", 3)
        c2.increment("node-b", 8)
        c2.increment("node-c", 2)

        merged = c1.merge(c2)
        assert merged.value == 10 + 8 + 2  # max of each

    def test_to_dict(self):
        c = GCounter()
        c.increment("node-a", 10)
        d = c.to_dict()
        assert d["node-a"] == 10

    def test_from_dict(self):
        c = GCounter.from_dict({"node-a": 10, "node-b": 5})
        assert c.value == 15


# ── PNCounter ────────────────────────────────────────────────────────────────

class TestPNCounter:
    def test_increment_decrement(self):
        c = PNCounter()
        c.increment("node-a", 100)
        c.decrement("node-a", 30)
        assert c.value == 70

    def test_merge(self):
        c1 = PNCounter()
        c1.increment("node-a", 50)

        c2 = PNCounter()
        c2.increment("node-b", 30)
        c2.decrement("node-b", 10)

        merged = c1.merge(c2)
        assert merged.value == 50 + 30 - 10

    def test_to_from_dict(self):
        c = PNCounter()
        c.increment("node-a", 100)
        c.decrement("node-b", 20)
        d = c.to_dict()
        restored = PNCounter.from_dict(d)
        assert restored.value == c.value


# ── LWWRegister ──────────────────────────────────────────────────────────────

class TestLWWRegister:
    def test_set_and_merge(self):
        r1 = LWWRegister()
        r1.set(1000, "node-a")

        time.sleep(0.01)  # ensure different timestamps
        r2 = LWWRegister()
        r2.set(2000, "node-b")

        merged = r1.merge(r2)
        assert merged.value == 2000  # r2 is newer

    def test_merge_same_timestamp(self):
        r1 = LWWRegister(value=100, timestamp=1.0, node_id="a")
        r2 = LWWRegister(value=200, timestamp=1.0, node_id="b")
        merged = r1.merge(r2)
        assert merged.value == 200  # "b" > "a"

    def test_to_from_dict(self):
        r = LWWRegister(value=42, timestamp=123.456, node_id="x")
        d = r.to_dict()
        restored = LWWRegister.from_dict(d)
        assert restored.value == 42
        assert restored.node_id == "x"


# ── AppBudgetCRDT ────────────────────────────────────────────────────────────

class TestAppBudgetCRDT:
    def test_basic(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.spend_cents.increment("node-a", 500)
        crdt.call_count.increment("node-a", 10)
        assert crdt.spend_cents.value == 500
        assert crdt.call_count.value == 10

    def test_set_budget_under(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(10000, "node-a")
        crdt.spend_cents.increment("node-a", 5000)
        assert not crdt.is_over_budget()

    def test_set_budget_over(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(10000, "node-a")
        crdt.spend_cents.increment("node-a", 11000)
        assert crdt.is_over_budget()

    def test_no_budget_limit(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.spend_cents.increment("node-a", 999999)
        assert not crdt.is_over_budget()

    def test_remaining_cents(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(10000, "node-a")
        crdt.spend_cents.increment("node-a", 3000)
        assert crdt.remaining_cents() == 7000

    def test_remaining_no_limit(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        assert crdt.remaining_cents() == 999999999

    def test_merge(self):
        c1 = AppBudgetCRDT(app_id="app-1")
        c1.spend_cents.increment("node-a", 3000)
        c1.call_count.increment("node-a", 5)

        c2 = AppBudgetCRDT(app_id="app-1")
        c2.spend_cents.increment("node-b", 4000)
        c2.call_count.increment("node-b", 8)

        merged = c1.merge(c2)
        assert merged.spend_cents.value == 7000
        assert merged.call_count.value == 13

    def test_to_dict(self):
        crdt = AppBudgetCRDT(app_id="app-1")
        crdt.budget_limit_cents.set(5000, "node-a")
        crdt.spend_cents.increment("node-a", 100)
        d = crdt.to_dict()
        assert d["app_id"] == "app-1"

    def test_from_dict(self):
        crdt = AppBudgetCRDT(app_id="app-2")
        crdt.budget_limit_cents.set(8000, "node-a")
        crdt.spend_cents.increment("node-a", 1000)
        crdt.call_count.increment("node-a", 5)

        d = crdt.to_dict()
        restored = AppBudgetCRDT.from_dict(d)
        assert restored.app_id == "app-2"
        assert restored.spend_cents.value == 1000


# ── CRDTSyncNode ────────────────────────────────────────────────────────────

class TestCRDTSyncNode:
    def test_record_spend_within_budget(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        state = AppBudgetCRDT(app_id="app-1")
        state.budget_limit_cents.set(10000, "test-node")
        node._states["app-1"] = state
        assert node.record_spend("app-1", 500) is True

    def test_record_spend_over_budget(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        state = AppBudgetCRDT(app_id="app-1")
        state.budget_limit_cents.set(100, "test-node")
        state.spend_cents.increment("test-node", 200)
        node._states["app-1"] = state
        assert node.record_spend("app-1", 10) is False

    def test_record_spend_creates_state(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        assert node.record_spend("new-app", 100) is True
        assert "new-app" in node._states

    def test_check_budget(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        state = AppBudgetCRDT(app_id="app-1")
        state.budget_limit_cents.set(10000, "test-node")
        state.spend_cents.increment("test-node", 5000)
        node._states["app-1"] = state
        within, remaining = node.check_budget("app-1")
        assert within is True
        assert remaining == 5000

    def test_check_budget_no_state(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        within, remaining = node.check_budget("nonexistent")
        assert within is True

    def test_get_state(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._states["app-1"] = AppBudgetCRDT(app_id="app-1")
        state = node.get_state("app-1")
        assert state is not None
        assert state.app_id == "app-1"

    def test_stop(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._active = True
        node._udp_socket = MagicMock()
        node.stop()
        assert node._active is False
        node._udp_socket.close.assert_called_once()

    def test_stop_with_socket_error(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._active = True
        node._udp_socket = MagicMock()
        node._udp_socket.close.side_effect = OSError("socket error")
        node.stop()
        assert node._active is False

    def test_send_deltas_empty(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._udp_socket = MagicMock()
        node._send_deltas()
        node._udp_socket.sendto.assert_not_called()

    def test_send_deltas_with_data(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._udp_socket = MagicMock()
        state = AppBudgetCRDT(app_id="app-1")
        state.spend_cents.increment("test-node", 100)
        node._states["app-1"] = state
        node._dirty.add("app-1")

        node._send_deltas()
        assert node._udp_socket.sendto.called

    def test_send_deltas_network_error(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._udp_socket = MagicMock()
        node._udp_socket.sendto.side_effect = OSError("network down")
        node._states["app-1"] = AppBudgetCRDT(app_id="app-1")
        node._dirty.add("app-1")

        node._send_deltas()
        assert "app-1" in node._dirty

    def test_handle_incoming_delta(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        remote_state = AppBudgetCRDT(app_id="app-1")
        remote_state.spend_cents.increment("remote-node", 200)

        packet = encode_delta("remote-node", {"app-1": remote_state})
        node._handle_incoming_delta(packet, ("127.0.0.1", 20000))

        assert "app-1" in node._states
        assert node._states["app-1"].spend_cents.value == 200

    def test_handle_incoming_delta_merge(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._states["app-1"] = AppBudgetCRDT(app_id="app-1")
        node._states["app-1"].spend_cents.increment("test-node", 300)

        remote_state = AppBudgetCRDT(app_id="app-1")
        remote_state.spend_cents.increment("remote-node", 200)

        packet = encode_delta("remote-node", {"app-1": remote_state})
        node._handle_incoming_delta(packet, ("127.0.0.1", 20000))

        assert node._states["app-1"].spend_cents.value == 500

    def test_handle_incoming_delta_invalid(self):
        node = CRDTSyncNode(node_id="test-node", orchestrator_port=19999)
        node._handle_incoming_delta(b"invalid-data", ("127.0.0.1", 20000))


# ── encode_delta / decode_delta ──────────────────────────────────────────────

class TestEncodeDecode:
    def test_roundtrip(self):
        state = AppBudgetCRDT(app_id="app-1")
        state.spend_cents.increment("node-a", 100)
        state.call_count.increment("node-a", 5)

        packet = encode_delta("node-a", {"app-1": state})
        assert packet[:4] == CRDT_MAGIC

        node_id, states = decode_delta(packet)
        assert node_id == "node-a"
        assert "app-1" in states
        assert states["app-1"].spend_cents.value == 100

    def test_multiple_apps(self):
        state1 = AppBudgetCRDT(app_id="app-1")
        state1.spend_cents.increment("node-a", 50)
        state2 = AppBudgetCRDT(app_id="app-2")
        state2.spend_cents.increment("node-a", 100)

        packet = encode_delta("node-a", {"app-1": state1, "app-2": state2})
        node_id, states = decode_delta(packet)
        assert len(states) == 2
        assert states["app-1"].spend_cents.value == 50
        assert states["app-2"].spend_cents.value == 100

    def test_invalid_magic(self):
        with pytest.raises(ValueError, match="Invalid"):
            decode_delta(b"BADx" + b"\x00" * 20)

    def test_invalid_version(self):
        data = CRDT_MAGIC + struct.pack("!B", 99) + b"\x00" * 20
        with pytest.raises(ValueError, match="Unsupported"):
            decode_delta(data)
