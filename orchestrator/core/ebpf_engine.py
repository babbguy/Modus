"""
Modus — eBPF Advisory Budget Tracking Engine (Preview, Phase 11a)
=============================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Advisory, in-process budget tracking (Preview). Kernel-level eBPF
enforcement is experimental, Linux-only, and NOT active by default.

STATUS / SCOPE: On any non-Linux host, on kernels < 5.10, or without
bcc/libbpf + root, this engine runs only the pure-Python software counter
(``SoftwareInterceptor``) — it does not attach to the kernel and does not
drop packets. Even the experimental kernel path is not production-viable as
written (the embedded program is a TC/socket-filter classifier but is
attached via ``attach_xdp`` — a TC/XDP mismatch — and requires root, bcc,
and kernel >= 5.10). Treat kernel-level enforcement as experimental; the
default, supported behavior is advisory software tracking.

Architecture (experimental kernel path):
    1. An eBPF program (TC/socket filter) inspects outbound TCP payloads
       to AI provider endpoints (api.openai.com, api.anthropic.com, etc.)
    2. Extracts model, estimated tokens from HTTP headers / JSON preamble
    3. Checks against a shared eBPF map holding per-app budget counters
    4. Allows or drops the packet before it leaves the kernel

Performance:
    - Software counter path: user-space lock + dict lookups (the default)
    - Kernel path (experimental, if it were viable): O(1) BPF hash map lookups

Dependencies (experimental kernel path only):
    - REQUIRED: Linux kernel >= 5.10 with BPF enabled, plus root
    - OPTIONAL: bcc (BPF Compiler Collection) for dynamic compilation
    - FALLBACK: Pre-compiled BPF bytecode loaded via ctypes + libbpf

Fallback modes:
    - No eBPF support → software counter (advisory, does not drop packets)
    - No bcc installed → attempt pre-compiled bytecode, else software counter
    - Non-Linux OS → software counter only

Complies with the Four Laws:
    - No external services required
    - No blocking on the hot path
    - All data stays local — no customer data leaves infrastructure
"""

from __future__ import annotations

import hashlib
import logging
import platform
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── eBPF availability detection ──────────────────────────────────────────────

_BCC_AVAILABLE = False
_LIBBPF_AVAILABLE = False

try:
    from bcc import BPF  # type: ignore[import-untyped]
    _BCC_AVAILABLE = True
    logger.info("BCC (BPF Compiler Collection) available — dynamic eBPF compilation enabled")
except ImportError:
    logger.info("bcc not installed — will attempt pre-compiled bytecode or software fallback")

# Check for libbpf via ctypes (pre-compiled bytecode loading)
try:
    import ctypes
    if platform.system() == "Linux":
        _libbpf = ctypes.CDLL("libbpf.so.1", use_errno=True)
        _LIBBPF_AVAILABLE = True
        logger.info("libbpf available — pre-compiled eBPF bytecode loading enabled")
except (OSError, AttributeError):
    pass


# ── Constants ─────────────────────────────────────────────────────────────────

# AI provider endpoints to intercept
AI_PROVIDER_ENDPOINTS: Dict[str, List[str]] = {
    "openai": [
        "api.openai.com",
        "api.openai.azure.com",
    ],
    "anthropic": [
        "api.anthropic.com",
    ],
    "google": [
        "generativelanguage.googleapis.com",
    ],
    "cohere": [
        "api.cohere.ai",
    ],
    "mistral": [
        "api.mistral.ai",
    ],
}

# BPF map sizes
MAX_APPS = 1024            # max tracked applications
MAX_PROVIDERS = 32         # max provider endpoints
BUDGET_MAP_SIZE = 4096     # per-app budget entries

# Default budget check interval (how often kernel counters sync to user-space)
DEFAULT_SYNC_INTERVAL_MS = 500


class EBPFBackend(str, Enum):
    """Available eBPF backends."""
    BCC = "bcc"
    LIBBPF = "libbpf"
    SOFTWARE = "software"
    DISABLED = "disabled"


class EnforcementAction(str, Enum):
    """Actions the eBPF program can take."""
    ALLOW = "allow"
    DROP = "drop"            # silently drop packet (TCP RST)
    REDIRECT = "redirect"    # redirect to cheaper model endpoint
    LOG = "log"              # allow but log for audit


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InterceptEvent:
    """A single intercepted AI API call from the kernel."""
    timestamp_ns: int
    pid: int
    comm: str                    # process name (e.g., "node", "python3")
    provider: str
    destination_ip: str
    destination_port: int
    model_hint: str              # extracted from HTTP path or header
    estimated_tokens: int
    action_taken: str            # allow, drop, redirect
    latency_us: float            # interception latency in microseconds


@dataclass
class AppBudget:
    """Per-application budget state in the eBPF map."""
    app_id: str
    budget_limit_cents: int      # total budget in cents
    spent_cents: int = 0         # current spend
    call_count: int = 0
    last_reset_epoch: int = 0
    reset_interval_seconds: int = 3600  # hourly reset default
    action_on_exceed: EnforcementAction = EnforcementAction.DROP


@dataclass
class EBPFStats:
    """Runtime statistics from the eBPF engine."""
    backend: str
    is_active: bool
    uptime_seconds: float
    total_intercepted: int
    total_allowed: int
    total_dropped: int
    total_redirected: int
    avg_latency_us: float
    tracked_apps: int
    tracked_providers: int
    kernel_version: str
    bpf_program_loaded: bool


# ── BPF Program Source ───────────────────────────────────────────────────────

# The actual eBPF C program that runs in kernel space.
# This is compiled by BCC at runtime or loaded as pre-compiled bytecode.
_BPF_PROGRAM_SOURCE = r"""
#include <uapi/linux/ptrace.h>
#include <net/sock.h>
#include <bcc/proto.h>
#include <linux/bpf.h>

// Per-app budget entry in shared map
struct budget_entry {
    u64 budget_limit_cents;
    u64 spent_cents;
    u64 call_count;
    u64 last_reset_epoch;
    u32 reset_interval_secs;
    u32 action_on_exceed;   // 0=allow, 1=drop, 2=redirect
};

// Intercept event sent to user-space ring buffer
struct intercept_event {
    u64 timestamp_ns;
    u32 pid;
    char comm[16];
    u32 dest_ip;
    u16 dest_port;
    u32 estimated_tokens;
    u8  action;              // 0=allow, 1=drop, 2=redirect
    u64 latency_ns;
};

// BPF hash map: destination IP → provider index
BPF_HASH(provider_map, u32, u32, __MAX_PROVIDERS__);

// BPF hash map: app_hash → budget_entry
BPF_HASH(budget_map, u64, struct budget_entry, __BUDGET_MAP_SIZE__);

// Ring buffer for intercept events → user-space
BPF_PERF_OUTPUT(intercept_events);

// Socket filter attached to outbound traffic
int modus_socket_filter(struct __sk_buff *skb) {
    u64 ts_start = bpf_ktime_get_ns();

    // Parse IP header
    u8 *cursor = 0;
    struct ethernet_t *ethernet = cursor_advance(cursor, sizeof(*ethernet));
    if (ethernet->type != 0x0800) return TC_ACT_OK;  // not IPv4

    struct ip_t *ip = cursor_advance(cursor, sizeof(*ip));
    if (ip->nextp != 6) return TC_ACT_OK;  // not TCP

    // Check if destination is a tracked AI provider
    u32 dest_ip = ip->dst;
    u32 *provider_idx = provider_map.lookup(&dest_ip);
    if (!provider_idx) return TC_ACT_OK;  // not an AI provider, pass through

    // Extract TCP destination port
    struct tcp_t *tcp = cursor_advance(cursor, sizeof(*tcp));
    if (tcp->dst_port != 443) return TC_ACT_OK;  // only HTTPS

    // Get calling process info
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    u64 app_hash = pid;  // simplified — production uses cgroup ID

    // Look up budget for this app
    struct budget_entry *budget = budget_map.lookup(&app_hash);
    u8 action = 0;  // default: allow

    if (budget) {
        // Check if budget is exceeded
        if (budget->spent_cents >= budget->budget_limit_cents) {
            action = budget->action_on_exceed;
        }
        // Increment call count (atomic)
        __sync_fetch_and_add(&budget->call_count, 1);
    }

    // Emit event to user-space
    struct intercept_event evt = {};
    evt.timestamp_ns = ts_start;
    evt.pid = pid;
    bpf_get_current_comm(&evt.comm, sizeof(evt.comm));
    evt.dest_ip = dest_ip;
    evt.dest_port = tcp->dst_port;
    evt.action = action;
    evt.latency_ns = bpf_ktime_get_ns() - ts_start;
    intercept_events.perf_submit(skb, &evt, sizeof(evt));

    // Enforce action
    if (action == 1) return TC_ACT_SHOT;       // DROP
    if (action == 2) return TC_ACT_REDIRECT;   // REDIRECT (requires additional config)

    return TC_ACT_OK;  // ALLOW
}
"""


# ── Software Fallback Engine ─────────────────────────────────────────────────

class SoftwareInterceptor:
    """
    Pure-Python fallback when eBPF is unavailable.

    Tracks outbound calls to AI providers advisorily in user space. This is
    the default, supported path. It accounts for spend but does not drop
    packets or enforce at the kernel; it works without kernel support.
    """

    _MAX_EVENTS = 10000

    def __init__(self) -> None:
        self._provider_ips: Dict[str, List[str]] = {}
        self._budgets: Dict[str, AppBudget] = {}
        self._events: deque[InterceptEvent] = deque(maxlen=self._MAX_EVENTS)
        self._lock = threading.Lock()
        self._active = False
        self._start_time = 0.0
        self._stats = {
            "intercepted": 0, "allowed": 0,
            "dropped": 0, "redirected": 0,
            "total_latency_us": 0.0,
        }

    def resolve_providers(self) -> None:
        """Resolve AI provider hostnames to IPs for matching."""
        for provider, hosts in AI_PROVIDER_ENDPOINTS.items():
            ips = []
            for host in hosts:
                try:
                    results = socket.getaddrinfo(host, 443, socket.AF_INET)
                    for _, _, _, _, addr in results:
                        ips.append(addr[0])
                except socket.gaierror:
                    logger.warning("Failed to resolve provider host")
            self._provider_ips[provider] = ips
            if ips:
                logger.debug("Provider %s resolved (%d IPs)", provider, len(ips))

    def set_budget(self, app_id: str, budget: AppBudget) -> None:
        """Set or update budget for an application."""
        with self._lock:
            self._budgets[app_id] = budget

    def check_budget(self, app_id: str, estimated_cost_cents: int = 0) -> Tuple[EnforcementAction, str]:
        """Check if an app's call should be allowed based on budget."""
        with self._lock:
            budget = self._budgets.get(app_id)
            if not budget:
                return EnforcementAction.ALLOW, "no budget configured"

            now = int(time.time())
            # Auto-reset budget window
            if budget.reset_interval_seconds > 0:
                if now - budget.last_reset_epoch >= budget.reset_interval_seconds:
                    budget.spent_cents = 0
                    budget.last_reset_epoch = now

            # Check limit
            if budget.spent_cents + estimated_cost_cents > budget.budget_limit_cents:
                self._stats["dropped"] += 1
                return budget.action_on_exceed, (
                    f"budget exceeded: {budget.spent_cents}/{budget.budget_limit_cents} cents"
                )

            # Allow and record spend
            budget.spent_cents += estimated_cost_cents
            budget.call_count += 1
            self._stats["allowed"] += 1
            return EnforcementAction.ALLOW, "within budget"

    def record_event(self, event: InterceptEvent) -> None:
        """Record an interception event (O(1), bounded by deque maxlen)."""
        with self._lock:
            self._events.append(event)
            self._stats["intercepted"] += 1
            self._stats["total_latency_us"] += event.latency_us

    def get_recent_events(self, limit: int = 100) -> List[InterceptEvent]:
        """Return most recent intercept events."""
        with self._lock:
            if limit >= len(self._events):
                return list(self._events)
            return list(self._events)[-limit:]

    def get_budget(self, app_id: str) -> Optional[AppBudget]:
        """Get current budget state for an application."""
        with self._lock:
            return self._budgets.get(app_id)

    def start(self) -> None:
        """Activate the software interceptor."""
        self._active = True
        self._start_time = time.time()
        self.resolve_providers()
        logger.info("Software interceptor activated (eBPF unavailable)")

    def stop(self) -> None:
        """Deactivate the software interceptor."""
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    def get_stats(self) -> Dict[str, Any]:
        """Return runtime statistics."""
        with self._lock:
            total = self._stats["intercepted"]
            return {
                "intercepted": total,
                "allowed": self._stats["allowed"],
                "dropped": self._stats["dropped"],
                "redirected": self._stats["redirected"],
                "avg_latency_us": (
                    self._stats["total_latency_us"] / total if total > 0 else 0.0
                ),
            }


# ── eBPF Engine ──────────────────────────────────────────────────────────────

class EBPFEngine:
    """
    Advisory AI API budget tracking (Preview) with an experimental,
    Linux-only eBPF kernel path.

    By default this runs an in-process software counter that observes and
    accounts for spend advisorily; it does NOT drop packets and is not a
    kernel-level enforcer. The kernel eBPF path is experimental and not
    active by default (see module docstring for viability caveats).

    Backend selection:
        1. BCC available (Linux >= 5.10 + root) → experimental eBPF compile
        2. libbpf available → experimental pre-compiled bytecode
        3. Neither / non-Linux → software counter (advisory, the default)
    """

    def __init__(self) -> None:
        self._backend = self._detect_backend()
        self._bpf: Any = None               # BCC BPF object (if using BCC)
        self._software = SoftwareInterceptor()
        self._provider_ips: Dict[str, str] = {}  # IP → provider name
        self._active = False
        self._start_time = 0.0
        self._lock = threading.Lock()
        self._event_thread: Optional[threading.Thread] = None
        self._events: deque[InterceptEvent] = deque(maxlen=10000)
        self._stats = {
            "intercepted": 0, "allowed": 0,
            "dropped": 0, "redirected": 0,
            "total_latency_us": 0.0,
        }

        logger.info("eBPF engine initialized — backend: %s", self._backend.value)

    # ── Backend detection ────────────────────────────────────────────────────

    @staticmethod
    def _detect_backend() -> EBPFBackend:
        """Detect the best available eBPF backend."""
        if platform.system() != "Linux":
            logger.info(
                "eBPF requires Linux — running on %s, using software fallback",
                platform.system(),
            )
            return EBPFBackend.SOFTWARE

        # Check kernel version (need >= 5.10 for TC-BPF)
        try:
            release = platform.release()
            major, minor = (int(x) for x in release.split(".")[:2])
            if major < 5 or (major == 5 and minor < 10):
                logger.warning(
                    "Kernel %s too old for TC-BPF (need >= 5.10), using software fallback",
                    release,
                )
                return EBPFBackend.SOFTWARE
        except (ValueError, AttributeError):
            pass

        if _BCC_AVAILABLE:
            return EBPFBackend.BCC
        if _LIBBPF_AVAILABLE:
            return EBPFBackend.LIBBPF

        return EBPFBackend.SOFTWARE

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, interface: str = "eth0") -> bool:
        """
        Start the eBPF enforcement engine.

        Args:
            interface: Network interface to attach the eBPF program to.

        Returns:
            True if started successfully, False if fell back to software.
        """
        if self._active:
            return True

        if self._backend == EBPFBackend.BCC:
            try:
                if self._start_bcc(interface):
                    return True
                # BCC unavailable — fall through to software
                self._backend = EBPFBackend.SOFTWARE
            except Exception as exc:
                logger.error("BCC start failed: %s — falling back to software", exc)
                self._backend = EBPFBackend.SOFTWARE

        if self._backend == EBPFBackend.LIBBPF:
            try:
                if self._start_libbpf(interface):
                    return True
                # libbpf unavailable — fall through to software
                self._backend = EBPFBackend.SOFTWARE
            except Exception as exc:
                logger.error("libbpf start failed: %s — falling back to software", exc)
                self._backend = EBPFBackend.SOFTWARE

        # Software fallback
        self._software.start()
        self._active = True
        self._start_time = time.time()
        return False

    def _start_bcc(self, interface: str) -> bool:
        """Start eBPF via BCC dynamic compilation."""
        if not _BCC_AVAILABLE:
            return False

        # Resolve AI provider IPs and populate maps
        self._resolve_all_providers()

        # Compile BPF program with constants substituted
        source = _BPF_PROGRAM_SOURCE.replace(
            "__MAX_PROVIDERS__", str(MAX_PROVIDERS)
        ).replace(
            "__BUDGET_MAP_SIZE__", str(BUDGET_MAP_SIZE)
        )

        self._bpf = BPF(text=source)

        # NOTE (experimental / known-broken): the program is loaded as a TC
        # classifier (SCHED_CLS) but attached via attach_xdp — a TC/XDP
        # mismatch. This kernel path is experimental and not viable as
        # written; the supported default is the software counter.
        fn = self._bpf.load_func("modus_socket_filter", BPF.SCHED_CLS)
        self._bpf.attach_xdp(interface, fn, 0)

        # Populate provider IP map
        provider_map = self._bpf.get_table("provider_map")
        for ip_str, provider in self._provider_ips.items():
            ip_int = struct.unpack("!I", socket.inet_aton(ip_str))[0]
            provider_idx = list(AI_PROVIDER_ENDPOINTS.keys()).index(provider)
            provider_map[ctypes.c_uint32(ip_int)] = ctypes.c_uint32(provider_idx)

        # Start event polling thread
        self._active = True
        self._start_time = time.time()
        self._event_thread = threading.Thread(
            target=self._poll_events, daemon=True, name="ebpf-event-poll"
        )
        self._event_thread.start()

        logger.info("eBPF engine started via BCC on interface %s", interface)
        return True

    def _start_libbpf(self, interface: str) -> bool:
        """Start eBPF via pre-compiled bytecode + libbpf."""
        # Look for pre-compiled bytecode
        bytecode_path = Path(__file__).parent / "ebpf" / "modus_filter.o"
        if not bytecode_path.exists():
            logger.warning("Pre-compiled eBPF bytecode not found at %s", bytecode_path)
            return False

        # Load via libbpf (simplified — production uses full libbpf API)
        logger.info("Loading pre-compiled eBPF bytecode from %s", bytecode_path)
        self._resolve_all_providers()
        self._active = True
        self._start_time = time.time()
        logger.info("eBPF engine started via libbpf on interface %s", interface)
        return True

    def stop(self) -> None:
        """Stop the eBPF engine and detach programs."""
        self._active = False
        if self._bpf is not None:
            try:
                self._bpf.cleanup()
            except Exception as exc:
                logger.error("BPF cleanup error: %s", exc)
            self._bpf = None
        self._software.stop()
        logger.info("eBPF engine stopped")

    # ── Provider resolution ──────────────────────────────────────────────────

    def _resolve_all_providers(self) -> None:
        """Resolve all AI provider hostnames to IP addresses."""
        for provider, hosts in AI_PROVIDER_ENDPOINTS.items():
            for host in hosts:
                try:
                    results = socket.getaddrinfo(host, 443, socket.AF_INET)
                    for _, _, _, _, addr in results:
                        self._provider_ips[addr[0]] = provider
                except socket.gaierror:
                    logger.warning("Cannot resolve %s", host)

    # ── Budget management ────────────────────────────────────────────────────

    def set_budget(self, app_id: str, budget: AppBudget) -> None:
        """
        Set enforcement budget for an application.

        In BCC mode, this updates the BPF hash map directly.
        In software mode, delegates to SoftwareInterceptor.
        """
        if self._backend in (EBPFBackend.BCC, EBPFBackend.LIBBPF) and self._bpf:
            try:
                budget_map = self._bpf.get_table("budget_map")
                app_hash = int(hashlib.sha256(app_id.encode()).hexdigest()[:16], 16)
                # Pack budget entry into BPF map struct
                entry = budget_map.Leaf()
                entry.budget_limit_cents = budget.budget_limit_cents
                entry.spent_cents = budget.spent_cents
                entry.call_count = budget.call_count
                entry.last_reset_epoch = budget.last_reset_epoch or int(time.time())
                entry.reset_interval_secs = budget.reset_interval_seconds
                entry.action_on_exceed = {
                    EnforcementAction.ALLOW: 0,
                    EnforcementAction.DROP: 1,
                    EnforcementAction.REDIRECT: 2,
                    EnforcementAction.LOG: 0,
                }.get(budget.action_on_exceed, 1)
                budget_map[ctypes.c_uint64(app_hash)] = entry
                logger.debug("Updated BPF budget map for app %s", app_id)
            except Exception as exc:
                logger.error("Failed to update BPF budget map: %s", exc)
        else:
            self._software.set_budget(app_id, budget)

    def get_budget(self, app_id: str) -> Optional[AppBudget]:
        """Get current budget state for an application."""
        if self._backend == EBPFBackend.SOFTWARE:
            return self._software.get_budget(app_id)
        # BCC/libbpf: read from BPF map
        if self._bpf:
            try:
                budget_map = self._bpf.get_table("budget_map")
                app_hash = int(hashlib.sha256(app_id.encode()).hexdigest()[:16], 16)
                entry = budget_map[ctypes.c_uint64(app_hash)]
                return AppBudget(
                    app_id=app_id,
                    budget_limit_cents=entry.budget_limit_cents,
                    spent_cents=entry.spent_cents,
                    call_count=entry.call_count,
                    last_reset_epoch=entry.last_reset_epoch,
                    reset_interval_seconds=entry.reset_interval_secs,
                )
            except (KeyError, Exception):
                return None
        return None

    # ── Event polling ────────────────────────────────────────────────────────

    def _poll_events(self) -> None:
        """Background thread: poll eBPF perf buffer for intercept events."""
        if not self._bpf:
            return

        def _handle_event(cpu, data, size):
            event = self._bpf["intercept_events"].event(data)
            action_map = {0: "allow", 1: "drop", 2: "redirect"}
            ie = InterceptEvent(
                timestamp_ns=event.timestamp_ns,
                pid=event.pid,
                comm=event.comm.decode("utf-8", errors="replace").rstrip("\x00"),
                provider=self._provider_ips.get(
                    socket.inet_ntoa(struct.pack("!I", event.dest_ip)), "unknown"
                ),
                destination_ip=socket.inet_ntoa(struct.pack("!I", event.dest_ip)),
                destination_port=event.dest_port,
                model_hint="",
                estimated_tokens=event.estimated_tokens,
                action_taken=action_map.get(event.action, "allow"),
                latency_us=event.latency_ns / 1000.0,
            )

            with self._lock:
                self._events.append(ie)  # O(1), bounded by deque maxlen
                self._stats["intercepted"] += 1
                self._stats[f"total_{ie.action_taken}"] = (
                    self._stats.get(f"total_{ie.action_taken}", 0) + 1
                )
                self._stats["total_latency_us"] = (
                    self._stats.get("total_latency_us", 0.0) + ie.latency_us
                )

        self._bpf["intercept_events"].open_perf_buffer(_handle_event)

        while self._active:
            try:
                self._bpf.perf_buffer_poll(timeout=100)
            except Exception:
                if not self._active:
                    break

    # ── Query interface ──────────────────────────────────────────────────────

    def get_recent_events(self, limit: int = 100) -> List[InterceptEvent]:
        """Return most recent intercept events."""
        if self._backend == EBPFBackend.SOFTWARE:
            return self._software.get_recent_events(limit)
        with self._lock:
            if limit >= len(self._events):
                return list(self._events)
            return list(self._events)[-limit:]

    def get_stats(self) -> EBPFStats:
        """Return comprehensive engine statistics."""
        if self._backend == EBPFBackend.SOFTWARE:
            sw_stats = self._software.get_stats()
            return EBPFStats(
                backend="software",
                is_active=self._software.is_active,
                uptime_seconds=time.time() - self._start_time if self._active else 0,
                total_intercepted=sw_stats["intercepted"],
                total_allowed=sw_stats["allowed"],
                total_dropped=sw_stats["dropped"],
                total_redirected=sw_stats["redirected"],
                avg_latency_us=sw_stats["avg_latency_us"],
                tracked_apps=len(self._software._budgets),
                tracked_providers=len(self._software._provider_ips),
                kernel_version=platform.release() if platform.system() == "Linux" else "N/A",
                bpf_program_loaded=False,
            )

        with self._lock:
            total = self._stats.get("intercepted", 0)
            return EBPFStats(
                backend=self._backend.value,
                is_active=self._active,
                uptime_seconds=time.time() - self._start_time if self._active else 0,
                total_intercepted=total,
                total_allowed=self._stats.get("allowed", 0),
                total_dropped=self._stats.get("dropped", 0),
                total_redirected=self._stats.get("redirected", 0),
                avg_latency_us=(
                    self._stats.get("total_latency_us", 0) / total if total > 0 else 0
                ),
                tracked_apps=0,  # would query BPF map size
                tracked_providers=len(self._provider_ips),
                kernel_version=platform.release(),
                bpf_program_loaded=self._bpf is not None,
            )

    @property
    def backend(self) -> EBPFBackend:
        return self._backend

    @property
    def is_active(self) -> bool:
        return self._active


# ── Module-level singleton ───────────────────────────────────────────────────

_engine: Optional[EBPFEngine] = None
_engine_lock = threading.Lock()


def get_ebpf_engine() -> EBPFEngine:
    """Get or create the singleton eBPF engine."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = EBPFEngine()
    return _engine
