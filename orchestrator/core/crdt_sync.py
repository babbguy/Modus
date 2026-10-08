"""
Modus — CRDT Advisory Local Cache (Preview, Phase 11b)
==========================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

CRDT (Conflict-free Replicated Data Type) local cache for budget tracking
with gossip-based sync.

STATUS / SCOPE: This is an advisory local cache (Preview). It converges
toward the database of record; it is NOT a hard-cap enforcer under network
partition — while partitioned, independent nodes can each admit spend that
only reconciles (and may reveal an overspend) after gossip converges. Use it
to reduce hot-path database reads, not as the authoritative budget gate.

Architecture:
    1. Each Modus agent maintains a local PN-Counter CRDT for budget
       usage (increments = spend, decrements = refunds/resets)
    2. Local writes take an in-process lock and update dict-backed counters
       (no round-trip to a centralized database on the write itself)
    3. State deltas gossip to the orchestrator via UDP every N seconds
    4. Orchestrator merges CRDT states and persists them to a JSON side-file
       (full SQLite integration is unfinished — see ``_persist_to_db``)
    5. If network drops, agents keep tracking against local state (advisory)

CRDT Types Used:
    - PNCounter: positive-negative counter for budget tracking
      (monotonically increasing increments + decrements, merge = max)
    - GCounter: grow-only counter for call counts
    - LWWRegister: last-writer-wins register for policy versions

Performance (indicative, not guaranteed):
    - Local budget check: a lock acquisition plus a few dict lookups
    - Delta encoding: compact binary protocol
    - UDP gossip: non-blocking, fire-and-forget
    - Merge on receive: element-wise max

Complies with the Four Laws:
    - Pure Python, stdlib only — no external CRDT libraries
    - No database round-trip on the local counter check
    - UDP gossip is lightweight (~100 bytes per delta)
    - All data stays local — gossip only to customer's own orchestrator
    - Works fully air-gapped (local tracking continues indefinitely)
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_GOSSIP_PORT = 9471
DEFAULT_GOSSIP_INTERVAL_MS = 2000     # sync every 2 seconds
MAX_DELTA_SIZE_BYTES = 65507          # max UDP payload
CRDT_MAGIC = b"CNTL"                 # packet magic bytes
CRDT_VERSION = 1


class MergeStrategy(str, Enum):
    """How to resolve concurrent updates."""
    MAX = "max"           # standard CRDT merge (element-wise max)
    SUM = "sum"           # additive merge (for counters)
    LWW = "lww"           # last-writer-wins (for registers)


# ── CRDT Primitives ──────────────────────────────────────────────────────────


@dataclass
class GCounter:
    """
    Grow-only Counter (G-Counter) CRDT.

    Each node maintains its own increment count. The global value is the
    sum of all node counts. Merge is element-wise max.
    """
    counts: Dict[str, int] = field(default_factory=dict)

    def increment(self, node_id: str, amount: int = 1) -> None:
        """Increment this node's counter."""
        self.counts[node_id] = self.counts.get(node_id, 0) + amount

    @property
    def value(self) -> int:
        """Global counter value (sum of all nodes)."""
        return sum(self.counts.values())

    def merge(self, other: GCounter) -> GCounter:
        """Merge with another G-Counter (element-wise max)."""
        all_nodes = set(self.counts) | set(other.counts)
        merged = GCounter()
        for node in all_nodes:
            merged.counts[node] = max(
                self.counts.get(node, 0),
                other.counts.get(node, 0),
            )
        return merged

    def to_dict(self) -> Dict[str, int]:
        return dict(self.counts)

    @classmethod
    def from_dict(cls, data: Dict[str, int]) -> GCounter:
        return cls(counts=dict(data))


@dataclass
class PNCounter:
    """
    Positive-Negative Counter (PN-Counter) CRDT.

    Composed of two G-Counters: one for increments (P) and one for
    decrements (N). Value = P.value - N.value. Merge merges both
    G-Counters independently.

    Used for budget tracking: increments = spend, decrements = refunds.
    """
    p: GCounter = field(default_factory=GCounter)  # positive (spend)
    n: GCounter = field(default_factory=GCounter)  # negative (refunds)

    def increment(self, node_id: str, amount: int = 1) -> None:
        """Record a spend increase."""
        self.p.increment(node_id, amount)

    def decrement(self, node_id: str, amount: int = 1) -> None:
        """Record a refund/reset."""
        self.n.increment(node_id, amount)

    @property
    def value(self) -> int:
        """Net counter value (spend - refunds)."""
        return self.p.value - self.n.value

    def merge(self, other: PNCounter) -> PNCounter:
        """Merge with another PN-Counter."""
        return PNCounter(
            p=self.p.merge(other.p),
            n=self.n.merge(other.n),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"p": self.p.to_dict(), "n": self.n.to_dict()}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PNCounter:
        return cls(
            p=GCounter.from_dict(data.get("p", {})),
            n=GCounter.from_dict(data.get("n", {})),
        )


@dataclass
class LWWRegister:
    """
    Last-Writer-Wins Register (LWW-Register) CRDT.

    Stores a value with a timestamp. On merge, the value with the
    higher timestamp wins. Used for policy version tracking.
    """
    value: Any = None
    timestamp: float = 0.0
    node_id: str = ""

    def set(self, value: Any, node_id: str) -> None:
        """Update the register value."""
        self.value = value
        self.timestamp = time.time()
        self.node_id = node_id

    def merge(self, other: LWWRegister) -> LWWRegister:
        """Merge: highest timestamp wins, ties broken by node_id."""
        if other.timestamp > self.timestamp:
            return LWWRegister(other.value, other.timestamp, other.node_id)
        if other.timestamp == self.timestamp and other.node_id > self.node_id:
            return LWWRegister(other.value, other.timestamp, other.node_id)
        return LWWRegister(self.value, self.timestamp, self.node_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "timestamp": self.timestamp, "node_id": self.node_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LWWRegister:
        return cls(
            value=data.get("value"),
            timestamp=data.get("timestamp", 0.0),
            node_id=data.get("node_id", ""),
        )


# ── App Budget CRDT State ───────────────────────────────────────────────────

@dataclass
class AppBudgetCRDT:
    """
    Per-application budget state as a CRDT.

    Combines multiple CRDT types to track all budget-related state:
    - spend: PN-Counter (cents spent, can be refunded)
    - call_count: G-Counter (total API calls, never decremented)
    - budget_limit: LWW-Register (current budget cap, set by orchestrator)
    - policy_version: LWW-Register (latest policy hash)
    """
    app_id: str
    spend_cents: PNCounter = field(default_factory=PNCounter)
    call_count: GCounter = field(default_factory=GCounter)
    budget_limit_cents: LWWRegister = field(default_factory=LWWRegister)
    policy_version: LWWRegister = field(default_factory=LWWRegister)

    def is_over_budget(self) -> bool:
        """Check if current spend exceeds the budget limit."""
        limit = self.budget_limit_cents.value
        if limit is None or limit <= 0:
            return False  # no limit set
        return self.spend_cents.value >= limit

    def remaining_cents(self) -> int:
        """Remaining budget in cents."""
        limit = self.budget_limit_cents.value
        if limit is None or limit <= 0:
            return 999999999  # effectively unlimited
        return max(0, limit - self.spend_cents.value)

    def merge(self, other: AppBudgetCRDT) -> AppBudgetCRDT:
        """Merge with another app budget CRDT state."""
        return AppBudgetCRDT(
            app_id=self.app_id,
            spend_cents=self.spend_cents.merge(other.spend_cents),
            call_count=self.call_count.merge(other.call_count),
            budget_limit_cents=self.budget_limit_cents.merge(other.budget_limit_cents),
            policy_version=self.policy_version.merge(other.policy_version),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": self.app_id,
            "spend_cents": self.spend_cents.to_dict(),
            "call_count": self.call_count.to_dict(),
            "budget_limit_cents": self.budget_limit_cents.to_dict(),
            "policy_version": self.policy_version.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AppBudgetCRDT:
        return cls(
            app_id=data["app_id"],
            spend_cents=PNCounter.from_dict(data.get("spend_cents", {"p": {}, "n": {}})),
            call_count=GCounter.from_dict(data.get("call_count", {})),
            budget_limit_cents=LWWRegister.from_dict(data.get("budget_limit_cents", {})),
            policy_version=LWWRegister.from_dict(data.get("policy_version", {})),
        )


# ── Delta Encoding ───────────────────────────────────────────────────────────

def encode_delta(node_id: str, states: Dict[str, AppBudgetCRDT]) -> bytes:
    """
    Encode CRDT state deltas into a compact binary packet for UDP gossip.

    Packet format:
        4 bytes: magic ("CNTL")
        1 byte:  version
        2 bytes: node_id length
        N bytes: node_id (UTF-8)
        4 bytes: number of app states
        For each app state:
            2 bytes: app_id length
            N bytes: app_id (UTF-8)
            remaining: JSON-encoded CRDT state
    """
    parts = [CRDT_MAGIC, struct.pack("!B", CRDT_VERSION)]

    node_bytes = node_id.encode("utf-8")
    parts.append(struct.pack("!H", len(node_bytes)))
    parts.append(node_bytes)

    parts.append(struct.pack("!I", len(states)))

    for app_id, state in states.items():
        app_bytes = app_id.encode("utf-8")
        state_json = json.dumps(state.to_dict(), separators=(",", ":")).encode("utf-8")
        parts.append(struct.pack("!H", len(app_bytes)))
        parts.append(app_bytes)
        parts.append(struct.pack("!I", len(state_json)))
        parts.append(state_json)

    return b"".join(parts)


def decode_delta(data: bytes) -> Tuple[str, Dict[str, AppBudgetCRDT]]:
    """Decode a gossip delta packet back into CRDT states."""
    offset = 0

    # Validate magic
    if data[offset:offset + 4] != CRDT_MAGIC:
        raise ValueError("Invalid CRDT packet magic")
    offset += 4

    # Version
    version = struct.unpack("!B", data[offset:offset + 1])[0]
    if version != CRDT_VERSION:
        raise ValueError(f"Unsupported CRDT version: {version}")
    offset += 1

    # Node ID
    node_id_len = struct.unpack("!H", data[offset:offset + 2])[0]
    offset += 2
    node_id = data[offset:offset + node_id_len].decode("utf-8")
    offset += node_id_len

    # App states
    num_apps = struct.unpack("!I", data[offset:offset + 4])[0]
    offset += 4

    states: Dict[str, AppBudgetCRDT] = {}
    for _ in range(num_apps):
        app_id_len = struct.unpack("!H", data[offset:offset + 2])[0]
        offset += 2
        app_id = data[offset:offset + app_id_len].decode("utf-8")
        offset += app_id_len

        state_len = struct.unpack("!I", data[offset:offset + 4])[0]
        offset += 4
        state_json = data[offset:offset + state_len].decode("utf-8")
        offset += state_len

        state_data = json.loads(state_json)
        states[app_id] = AppBudgetCRDT.from_dict(state_data)

    return node_id, states


# ── CRDT Sync Node ───────────────────────────────────────────────────────────

class CRDTSyncNode:
    """
    Local-first CRDT node for budget enforcement with gossip sync.

    Each agent runs a CRDTSyncNode that:
    1. Maintains local CRDT state for all tracked apps
    2. Applies advisory budget checks locally (lock + dict lookups; not a
       hard cap under partition — see module docstring)
    3. Periodically gossips deltas to the orchestrator via UDP
    4. Receives and merges deltas from the orchestrator
    5. Continues operating if the network drops (local-first)
    """

    def __init__(
        self,
        node_id: Optional[str] = None,
        gossip_port: int = DEFAULT_GOSSIP_PORT,
        gossip_interval_ms: int = DEFAULT_GOSSIP_INTERVAL_MS,
        orchestrator_host: str = "127.0.0.1",
        orchestrator_port: int = DEFAULT_GOSSIP_PORT,
    ) -> None:
        self._node_id = node_id or self._generate_node_id()
        self._gossip_port = gossip_port
        self._gossip_interval_ms = gossip_interval_ms
        self._orchestrator_addr = (orchestrator_host, orchestrator_port)

        # Local CRDT state: app_id → AppBudgetCRDT
        self._states: Dict[str, AppBudgetCRDT] = {}
        self._lock = threading.Lock()

        # Dirty tracking for delta gossip (only send changed states)
        self._dirty: Set[str] = set()

        # Gossip threads
        self._active = False
        self._gossip_thread: Optional[threading.Thread] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._udp_socket: Optional[socket.socket] = None

        # Stats
        self._stats = {
            "local_checks": 0,
            "gossip_sent": 0,
            "gossip_received": 0,
            "merges": 0,
            "budget_denials": 0,
            "offline_enforcements": 0,
        }

        logger.info("CRDT sync node initialized — id: %s", self._node_id)

    @staticmethod
    def _generate_node_id() -> str:
        """Generate a unique node ID from hostname + PID + timestamp."""
        import os
        raw = f"{socket.gethostname()}-{os.getpid()}-{time.time_ns()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the CRDT sync node (gossip + listen threads)."""
        if self._active:
            return

        self._active = True

        # Create UDP socket for gossip
        self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._udp_socket.bind(("0.0.0.0", self._gossip_port))
        except OSError:
            # Port in use — use ephemeral port for sending
            self._udp_socket.bind(("0.0.0.0", 0))
            logger.warning(
                "Gossip port %d in use — using ephemeral port for sending",
                self._gossip_port,
            )
        self._udp_socket.settimeout(0.5)

        # Start gossip sender thread
        self._gossip_thread = threading.Thread(
            target=self._gossip_loop, daemon=True, name="crdt-gossip"
        )
        self._gossip_thread.start()

        # Start listener thread
        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="crdt-listen"
        )
        self._listen_thread.start()

        logger.info(
            "CRDT sync node started — gossip every %dms to %s",
            self._gossip_interval_ms, self._orchestrator_addr,
        )

    def stop(self) -> None:
        """Stop the CRDT sync node."""
        self._active = False
        if self._udp_socket:
            try:
                self._udp_socket.close()
            except OSError:
                pass  # socket already closed
        logger.info("CRDT sync node stopped")

    # ── Local advisory check (lock + dict ops) ─────────────────────────────

    def record_spend(self, app_id: str, cost_cents: int) -> bool:
        """
        Record a spend and apply an advisory budget check in one locked
        operation.

        Returns True if within the locally-known budget, False if over.
        This is the hot-path function — it takes an in-process lock and does
        a handful of dict operations. It is advisory: under network partition
        the local view can lag the database of record (see module docstring).
        """
        with self._lock:
            state = self._states.get(app_id)
            if state is None:
                # No CRDT state for this app — create one
                state = AppBudgetCRDT(app_id=app_id)
                self._states[app_id] = state

            # Check budget BEFORE recording spend
            if state.is_over_budget():
                self._stats["budget_denials"] += 1
                return False

            # Record spend locally
            state.spend_cents.increment(self._node_id, cost_cents)
            state.call_count.increment(self._node_id)
            self._dirty.add(app_id)
            self._stats["local_checks"] += 1
            return True

    def check_budget(self, app_id: str) -> Tuple[bool, int]:
        """
        Check if an app is within budget without recording spend.

        Returns (is_allowed, remaining_cents).
        """
        with self._lock:
            state = self._states.get(app_id)
            if state is None:
                return True, 999999999

            return not state.is_over_budget(), state.remaining_cents()

    def set_budget_limit(self, app_id: str, limit_cents: int) -> None:
        """Set the budget limit for an app (from orchestrator)."""
        with self._lock:
            if app_id not in self._states:
                self._states[app_id] = AppBudgetCRDT(app_id=app_id)
            self._states[app_id].budget_limit_cents.set(limit_cents, self._node_id)
            self._dirty.add(app_id)

    def get_state(self, app_id: str) -> Optional[AppBudgetCRDT]:
        """Get current CRDT state for an app."""
        with self._lock:
            return self._states.get(app_id)

    def get_all_states(self) -> Dict[str, AppBudgetCRDT]:
        """Get all CRDT states (for API responses)."""
        with self._lock:
            return dict(self._states)

    # ── Gossip protocol ─────────────────────────────────────────────────────

    def _gossip_loop(self) -> None:
        """Background thread: periodically send dirty deltas to orchestrator."""
        interval = self._gossip_interval_ms / 1000.0
        while self._active:
            try:
                time.sleep(interval)
                self._send_deltas()
            except Exception as exc:
                if self._active:
                    logger.debug("Gossip send error (will retry): %s", exc)
                    self._stats["offline_enforcements"] += 1

    def _send_deltas(self) -> None:
        """Send dirty CRDT state deltas to the orchestrator via UDP."""
        with self._lock:
            if not self._dirty:
                return
            dirty_states = {
                app_id: self._states[app_id]
                for app_id in self._dirty
                if app_id in self._states
            }
            self._dirty.clear()

        if not dirty_states:
            return

        try:
            packet = encode_delta(self._node_id, dirty_states)
            if len(packet) > MAX_DELTA_SIZE_BYTES:
                # Split into multiple packets if too large
                for app_id, state in dirty_states.items():
                    small_packet = encode_delta(self._node_id, {app_id: state})
                    self._udp_socket.sendto(small_packet, self._orchestrator_addr)
            else:
                self._udp_socket.sendto(packet, self._orchestrator_addr)

            self._stats["gossip_sent"] += 1
        except (OSError, socket.error) as exc:
            # Network down — re-mark as dirty for next attempt
            with self._lock:
                self._dirty.update(dirty_states.keys())
            logger.debug("Gossip send failed (offline mode): %s", exc)

    def _listen_loop(self) -> None:
        """Background thread: listen for CRDT deltas from orchestrator."""
        while self._active:
            try:
                data, addr = self._udp_socket.recvfrom(MAX_DELTA_SIZE_BYTES)
                if data[:4] != CRDT_MAGIC:
                    continue
                self._handle_incoming_delta(data, addr)
            except socket.timeout:
                continue
            except Exception as exc:
                if self._active:
                    logger.debug("Gossip listen error: %s", exc)

    def _handle_incoming_delta(self, data: bytes, addr: Tuple[str, int]) -> None:
        """Merge an incoming CRDT delta from another node."""
        try:
            remote_node_id, remote_states = decode_delta(data)
        except (ValueError, json.JSONDecodeError, struct.error) as exc:
            logger.warning("Invalid CRDT delta from %s: %s", addr, exc)
            return

        with self._lock:
            for app_id, remote_state in remote_states.items():
                local_state = self._states.get(app_id)
                if local_state is None:
                    self._states[app_id] = remote_state
                else:
                    self._states[app_id] = local_state.merge(remote_state)
                self._stats["merges"] += 1

        self._stats["gossip_received"] += 1
        logger.debug(
            "Merged CRDT delta from node %s (%d app states)",
            remote_node_id, len(remote_states),
        )

    # ── Stats / diagnostics ──────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Return CRDT sync statistics."""
        with self._lock:
            return {
                "node_id": self._node_id,
                "active": self._active,
                "tracked_apps": len(self._states),
                "dirty_apps": len(self._dirty),
                "gossip_interval_ms": self._gossip_interval_ms,
                "orchestrator_addr": f"{self._orchestrator_addr[0]}:{self._orchestrator_addr[1]}",
                **dict(self._stats),
            }

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def is_active(self) -> bool:
        return self._active


# ── Orchestrator-side CRDT Hub ───────────────────────────────────────────────

class CRDTHub:
    """
    Orchestrator-side CRDT merge point.

    Receives gossip deltas from all agent nodes, merges them into a
    global view, and periodically persists to a JSON side-file (SQLite
    integration is unfinished — see ``_persist_to_db``). Also broadcasts
    merged state back to agents so they converge.
    """

    def __init__(
        self,
        listen_port: int = DEFAULT_GOSSIP_PORT,
        persist_interval_seconds: float = 10.0,
    ) -> None:
        self._listen_port = listen_port
        self._persist_interval = persist_interval_seconds

        # Global merged state: app_id → AppBudgetCRDT
        self._global_state: Dict[str, AppBudgetCRDT] = {}
        self._lock = threading.Lock()

        # Known agent nodes
        self._known_nodes: Dict[str, Tuple[str, int]] = {}  # node_id → (host, port)

        # Listener
        self._active = False
        self._listen_thread: Optional[threading.Thread] = None
        self._persist_thread: Optional[threading.Thread] = None
        self._udp_socket: Optional[socket.socket] = None

        # Stats
        self._stats = {
            "deltas_received": 0,
            "merges_performed": 0,
            "broadcasts_sent": 0,
            "persist_cycles": 0,
        }

    def start(self) -> None:
        """Start the CRDT hub listener."""
        if self._active:
            return

        self._active = True

        self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_socket.bind(("0.0.0.0", self._listen_port))
        self._udp_socket.settimeout(1.0)

        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="crdt-hub-listen"
        )
        self._listen_thread.start()

        self._persist_thread = threading.Thread(
            target=self._persist_loop, daemon=True, name="crdt-hub-persist"
        )
        self._persist_thread.start()

        logger.info("CRDT Hub started on port %d", self._listen_port)

    def stop(self) -> None:
        """Stop the CRDT hub."""
        self._active = False
        if self._udp_socket:
            try:
                self._udp_socket.close()
            except OSError:
                pass  # socket already closed

    def _listen_loop(self) -> None:
        """Listen for deltas from agent nodes and merge."""
        while self._active:
            try:
                data, addr = self._udp_socket.recvfrom(MAX_DELTA_SIZE_BYTES)
                if data[:4] != CRDT_MAGIC:
                    continue

                node_id, states = decode_delta(data)
                self._known_nodes[node_id] = addr

                with self._lock:
                    for app_id, remote_state in states.items():
                        local = self._global_state.get(app_id)
                        if local is None:
                            self._global_state[app_id] = remote_state
                        else:
                            self._global_state[app_id] = local.merge(remote_state)
                        self._stats["merges_performed"] += 1

                self._stats["deltas_received"] += 1

                # Broadcast merged state back to all known nodes
                self._broadcast_state(exclude_node=node_id)

            except socket.timeout:
                continue
            except Exception as exc:
                if self._active:
                    logger.debug("CRDT Hub listen error: %s", exc)

    def _broadcast_state(self, exclude_node: Optional[str] = None) -> None:
        """Send merged global state to all known agent nodes."""
        with self._lock:
            if not self._global_state:
                return
            packet = encode_delta("hub", self._global_state)

        for node_id, addr in self._known_nodes.items():
            if node_id == exclude_node:
                continue
            try:
                self._udp_socket.sendto(packet, addr)
                self._stats["broadcasts_sent"] += 1
            except (OSError, socket.error):
                pass

    def _persist_loop(self) -> None:
        """Periodically persist global CRDT state to the JSON side-file."""
        while self._active:
            try:
                time.sleep(self._persist_interval)
                self._persist_to_db()
            except Exception as exc:
                if self._active:
                    logger.error("CRDT persist error: %s", exc)

    def _persist_to_db(self) -> None:
        """Persist current global state to a JSON side-file.

        NOTE: despite the name, this does NOT write to SQLite. SQLite/
        write_queue integration is unfinished; the current implementation
        writes a JSON snapshot side-file as interim persistence.
        """
        with self._lock:
            snapshot = {
                app_id: state.to_dict()
                for app_id, state in self._global_state.items()
            }

        if not snapshot:
            return

        # Write to a local JSON file as interim persistence.
        # TODO: full SQLite integration via the existing write_queue is unfinished.
        try:
            state_file = Path(__file__).parent.parent / "data" / "crdt_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(snapshot, separators=(",", ":")))
            self._stats["persist_cycles"] += 1
        except Exception as exc:
            logger.error("CRDT state persist failed: %s", exc)

    # ── Public API (used by API routers — no private member access) ────────

    def set_budget(self, app_id: str, limit_cents: int) -> None:
        """Set or update budget limit for an application (thread-safe)."""
        with self._lock:
            state = self._global_state.get(app_id)
            if state is None:
                state = AppBudgetCRDT(app_id=app_id)
            state.budget_limit_cents.set(limit_cents, "hub")
            existing = self._global_state.get(app_id)
            if existing:
                self._global_state[app_id] = existing.merge(state)
            else:
                self._global_state[app_id] = state

    def record_spend(self, app_id: str, cost_cents: int) -> int:
        """Record spend for an app and return new total (thread-safe)."""
        with self._lock:
            state = self._global_state.get(app_id)
            if state is None:
                state = AppBudgetCRDT(app_id=app_id)
                self._global_state[app_id] = state
            state.spend_cents.increment("hub", cost_cents)
            state.call_count.increment("hub")
            return state.spend_cents.value

    def get_global_state(self) -> Dict[str, AppBudgetCRDT]:
        """Get the current merged global state."""
        with self._lock:
            return dict(self._global_state)

    def get_app_state(self, app_id: str) -> Optional[AppBudgetCRDT]:
        """Get merged state for a specific app."""
        with self._lock:
            return self._global_state.get(app_id)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def listen_port(self) -> int:
        return self._listen_port

    @listen_port.setter
    def listen_port(self, port: int) -> None:
        self._listen_port = port

    @property
    def persist_interval(self) -> float:
        return self._persist_interval

    @persist_interval.setter
    def persist_interval(self, interval: float) -> None:
        self._persist_interval = interval

    def get_stats(self) -> Dict[str, Any]:
        """Return hub statistics."""
        return {
            "active": self._active,
            "listen_port": self._listen_port,
            "known_nodes": len(self._known_nodes),
            "tracked_apps": len(self._global_state),
            **dict(self._stats),
        }


# We need Path for the persist function
from pathlib import Path

# ── Module-level singletons ──────────────────────────────────────────────────

_node: Optional[CRDTSyncNode] = None
_hub: Optional[CRDTHub] = None
_init_lock = threading.Lock()


def get_crdt_node() -> CRDTSyncNode:
    """Get or create the singleton CRDT sync node."""
    global _node
    if _node is None:
        with _init_lock:
            if _node is None:
                _node = CRDTSyncNode()
    return _node


def get_crdt_hub() -> CRDTHub:
    """Get or create the singleton CRDT hub (orchestrator-side only)."""
    global _hub
    if _hub is None:
        with _init_lock:
            if _hub is None:
                _hub = CRDTHub()
    return _hub
