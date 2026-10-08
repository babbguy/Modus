# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Real-SDK traffic and exact reconciliation.

Several apps register at the same moment with a team registration token, send
usage through the real SDK (sessions, spans, denied calls, a burst), and every
figure the API and dashboard report is then compared with what was sent,
exactly, at 8 decimal places.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from .common import Client, Results, log

PHASE = "traffic"
DENIED_MODEL = "gate-denied-model"


# ── SDK log capture ───────────────────────────────────────────────────────────

class SdkLogCapture(logging.Handler):
    """Collects WARNING+ records from the SDK (logger "modus")."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[tuple[str, str, str]] = []   # (phase, level, message)
        self.phase = "setup"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        self.records.append((self.phase, record.levelname, msg))

    def matching(self, phase: str, *needles: str) -> list[str]:
        return [m for p, _, m in self.records if p == phase and any(n in m for n in needles)]


SDK_LOG = SdkLogCapture()


def install_sdk_logging() -> None:
    lg = logging.getLogger("modus")
    lg.addHandler(SDK_LOG)
    lg.setLevel(logging.INFO)
    lg.propagate = False


# ── Ledger: everything the gate sent ──────────────────────────────────────────

@dataclass
class Ledger:
    lock: threading.Lock = field(default_factory=threading.Lock)
    total: Decimal = Decimal("0")
    calls: int = 0
    team_cost: dict = field(default_factory=lambda: defaultdict(Decimal))
    team_calls: dict = field(default_factory=lambda: defaultdict(int))
    app_cost: dict = field(default_factory=lambda: defaultdict(Decimal))      # (team, app_id)
    app_calls: dict = field(default_factory=lambda: defaultdict(int))
    model_cost: dict = field(default_factory=lambda: defaultdict(Decimal))    # (team, provider, model)
    model_calls: dict = field(default_factory=lambda: defaultdict(int))
    sessions: dict = field(default_factory=dict)                               # sid -> [cost, calls]

    def add(self, team: str, app_id: str, provider: str, model: str, cost: Decimal,
            session_id: Optional[str] = None) -> None:
        with self.lock:
            self.total += cost
            self.calls += 1
            self.team_cost[team] += cost
            self.team_calls[team] += 1
            self.app_cost[(team, app_id)] += cost
            self.app_calls[(team, app_id)] += 1
            self.model_cost[(team, provider, model)] += cost
            self.model_calls[(team, provider, model)] += 1
            if session_id:
                s = self.sessions.setdefault(session_id, [Decimal("0"), 0])
                s[0] += cost
                s[1] += 1


def cost_for(i: int, salt: int) -> tuple[Decimal, Decimal]:
    """Deterministic 8-dp costs that do not add up to round numbers."""
    inp = Decimal("0.00001234") * ((i % 3) + 1)
    out = Decimal("0.00056789") * ((i + salt) % 7 + 1)
    return inp, out


class SdkApp:
    """One instrumented application: a real ModusAgent plus bookkeeping."""

    def __init__(self, url: str, team_token: str, team_id: str, app_id: str, ledger: Ledger,
                 aggregation: bool, salt: int, flush_interval: int = 5):
        from modus.agent import ModusAgent   # the repo's own SDK (sys.path set by run_gate)
        self.app_id = app_id
        self.team_id = team_id
        self.ledger = ledger
        self.salt = salt
        self.i = 0
        self.agent = ModusAgent(orchestrator_url=url, team_token=team_token, app_id=app_id,
                                app_name=app_id, environment="production",
                                flush_interval=flush_interval, aggregation_enabled=aggregation,
                                trace_sample_rate=0.05)
        self.cb_trips = 0
        orig = self.agent._maybe_trip_circuit_breaker

        def counted() -> None:
            before = self.agent._circuit_breaker_tripped_at
            orig()
            if before is None and self.agent._circuit_breaker_tripped_at is not None:
                self.cb_trips += 1
        self.agent._maybe_trip_circuit_breaker = counted   # observe only; behaviour unchanged

    def start(self) -> bool:
        self.agent.start()
        return bool(self.agent._api_key)

    @property
    def api_key(self) -> str:
        return self.agent._api_key

    def call(self, provider: str, model: str, session_id: Optional[str] = None,
             enforce: bool = True) -> Optional[str]:
        """One governed LLM call: enforce, then record the usage."""
        i = self.i
        self.i += 1
        suggested = None
        if enforce:
            suggested = self.agent.enforce(provider=provider, model=model, estimated_tokens=1000)
        inp, out = cost_for(i, self.salt)
        self.agent.record(provider=provider, model=model, input_tokens=1000 + i % 50,
                          output_tokens=100 + i % 20, input_cost=inp, output_cost=out,
                          duration_ms=100 + i % 400)
        self.ledger.add(self.team_id, self.app_id, provider, model, inp + out, session_id)
        return suggested

    def denied_attempt(self, model: str = DENIED_MODEL) -> tuple[bool, str]:
        from modus.agent import PolicyViolationError
        try:
            self.agent.enforce(provider="openai", model=model, estimated_tokens=10)
            return False, "allowed"
        except PolicyViolationError as e:
            return True, getattr(e, "reason", str(e))

    def flush(self) -> None:
        self.agent.flush()

    def pending_batches(self) -> int:
        return len(getattr(self.agent, "_pending_batches", []) or [])

    def stop(self) -> None:
        try:
            self.agent._shutdown.set()
            self.agent._shutdown_flush()
        except Exception as exc:   # report, never hide
            log(f"SDK shutdown of {self.app_id} raised {exc!r}")


def register_concurrently(apps: list[SdkApp]) -> list[str]:
    """Start every app at the same instant; return the ones that failed."""
    barrier = threading.Barrier(len(apps))
    failed: list[str] = []

    def go(a: SdkApp) -> None:
        barrier.wait()
        try:
            if not a.start():
                failed.append(a.app_id)
        except Exception as exc:
            failed.append(f"{a.app_id}: {exc!r}")

    threads = [threading.Thread(target=go, args=(a,)) for a in apps]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return failed


# ── The traffic run ───────────────────────────────────────────────────────────

@dataclass
class TrafficRun:
    team_id: str = ""
    team_slug: str = ""
    apps: list[SdkApp] = field(default_factory=list)
    deny_expected: int = 0
    deny_seen: int = 0
    deny_local: int = 0
    burst_elapsed: float = 0.0
    burst_denied: int = 0
    burst_allowed: int = 0
    sessions_done_at: float = 0.0
    errors: list[str] = field(default_factory=list)


APP_MODELS = {
    "gate-app-a": (True, [("openai", "gpt-4o"), ("anthropic", "claude-sonnet-4-5")]),
    "gate-app-b": (True, [("openai", "gpt-4o-mini")]),
    "gate-app-c": (False, [("anthropic", "claude-haiku-4-5"), ("openai", "gpt-4o")]),
}


def run_traffic(stack, admin: Client, master: Client, ledger: Ledger, results: Results,
                run_label: str, duration_s: float = 45.0) -> TrafficRun:
    tr = TrafficRun()
    SDK_LOG.phase = PHASE
    tr.team_slug = f"gate-traffic-{run_label}"
    team = admin.ok_json("POST", "/api/v1/teams", {"slug": tr.team_slug, "name": f"Gate traffic {run_label}"})
    tr.team_id = team["id"]
    token = master.ok_json("POST", f"/api/v1/teams/{tr.team_id}/registration-token", {})["registration_token"]
    admin.ok_json("POST", "/api/v1/policies", {
        "name": f"gate deny {DENIED_MODEL}", "scope": "team", "team_id": tr.team_id,
        "policy_type": "model_denylist", "effect": "deny", "config": {"models": [DENIED_MODEL]},
    })

    # 3 apps + the burst app register at the same instant.
    for n, (app_id, (aggregation, _)) in enumerate(APP_MODELS.items(), start=1):
        tr.apps.append(SdkApp(stack.base_url, token, tr.team_id, f"{app_id}-{run_label}", ledger, aggregation, n))
    burst = SdkApp(stack.base_url, token, tr.team_id, f"gate-burst-{run_label}", ledger, True, 4)
    tr.apps.append(burst)
    failed = register_concurrently(tr.apps)
    results.equal(PHASE, f"concurrent registration of {len(tr.apps)} apps: failures", [], failed)
    live = [a for a in tr.apps if a.api_key]

    lock = threading.Lock()
    start = time.time()
    deadline = start + duration_s
    session_until = start + 12   # sessions first, so attribution can close them early

    def worker(a: SdkApp, models: list[tuple[str, str]]) -> None:
        n = 0
        try:
            while time.time() < deadline:
                provider, model = models[n % len(models)]
                if n % 9 == 0 and time.time() < session_until:
                    sid = f"gate-{run_label}-{a.app_id}-{n}"
                    with a.agent.session(sid):
                        with a.agent.span("plan"):
                            a.call(provider, model, sid)
                        with a.agent.span("act"):
                            a.call(provider, model, sid)
                            a.call(provider, model, sid)
                    n += 3
                else:
                    if n % 20 == 7:
                        denied, reason = a.denied_attempt()
                        with lock:
                            tr.deny_expected += 1
                            tr.deny_seen += int(denied)
                            tr.deny_local += int("local policy" in reason)
                    a.call(provider, model)
                    n += 1
                time.sleep(0.15)
        except Exception as exc:
            tr.errors.append(f"{a.app_id}: {exc!r}")

    threads = []
    for a in live:
        if a is burst:
            continue
        models = APP_MODELS[a.app_id.rsplit("-", 1)[0]][1]
        threads.append(threading.Thread(target=worker, args=(a, models)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    tr.sessions_done_at = time.time()

    # Burst: 600 governed calls from one app as fast as the SDK allows.
    if burst.api_key:
        b0 = time.time()
        for i in range(600):
            if i % 10 == 0:
                denied, _ = burst.denied_attempt()
                tr.burst_denied += int(denied)
                continue
            try:
                burst.call("openai", "gpt-4o")
                tr.burst_allowed += 1
            except Exception as exc:
                tr.errors.append(f"burst call {i}: {exc!r}")
        tr.burst_elapsed = time.time() - b0

    for a in live:
        a.flush()

    results.equal(PHASE, "SDK traffic errors", [], tr.errors)
    results.add(PHASE, "denied model refused every time (main run)", tr.deny_seen == tr.deny_expected and tr.deny_expected > 0,
                f"{tr.deny_expected} denials", f"{tr.deny_seen} denials")
    results.add(PHASE, "denied model refused locally after SDK policy sync", tr.deny_local == tr.deny_expected and tr.deny_expected > 0,
                f"{tr.deny_expected} local denials", f"{tr.deny_local} local denials")
    results.add(PHASE, "burst: 600 governed calls in under 60 s", 0 < tr.burst_elapsed < 60, "< 60 s", f"{tr.burst_elapsed:.1f} s")
    results.equal(PHASE, "burst: denied-model attempts refused", 60, tr.burst_denied)
    results.equal(PHASE, "burst: allowed calls recorded", 540, tr.burst_allowed)
    log(f"traffic sent: {ledger.calls} calls, ${ledger.total} (burst {tr.burst_elapsed:.1f}s)")
    return tr


def sdk_health_checks(tr: TrafficRun, results: Results, phase: str = PHASE) -> None:
    """Zero 429s, circuit-breaker trips, failed flushes and leftover batches in the SDK."""
    apps = [a for a in tr.apps if a.api_key]
    results.equal(phase, "SDK circuit-breaker trips", 0, sum(a.cb_trips for a in apps))
    results.equal(phase, "SDK rate-limited (429) responses", 0,
                  sum(int(getattr(a.agent, "rate_limited_count", 0)) for a in apps))
    results.equal(phase, "SDK batches left unsent", 0, sum(a.pending_batches() for a in apps))
    bad = SDK_LOG.matching(PHASE, "circuit breaker", "429", "rate limit", "registration failed",
                           "flush failed", "unreachable", "returned HTTP", "evaluate error")
    results.equal(phase, "SDK warnings about the server during traffic", [], bad[:5])


# ── Reconciliation ────────────────────────────────────────────────────────────

def _dec(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("NaN")


VISIBLE_WITHIN_S = 10.0


def await_visible(admin: Client, ledger: Ledger, tr: "TrafficRun", results: Results) -> None:
    """Ingest is accepted (202) and written by the server's write queue, so a
    read straight after the SDK flush may briefly lag. Everything sent must be
    visible within VISIBLE_WITHIN_S seconds."""
    want = ledger.team_cost[tr.team_id]
    t0 = time.monotonic()
    seen = None
    while time.monotonic() - t0 < VISIBLE_WITHIN_S:
        seen = admin.ok_json("GET", f"/api/v1/dashboard/summary?team_id={tr.team_id}").get("total_cost_today")
        if seen is not None and _dec(seen) == want:
            break
        time.sleep(0.5)
    results.add("reconcile", f"all traffic visible within {VISIBLE_WITHIN_S:.0f} s of the SDK flush",
                seen is not None and _dec(seen) == want, str(want), f"{seen} after {time.monotonic() - t0:.1f} s")


def reconcile(admin: Client, ledger: Ledger, tr: TrafficRun, results: Results, label: str,
              include_global: bool) -> None:
    phase = "reconcile"
    team = tr.team_id
    exp_cost = ledger.team_cost[team]
    exp_calls = ledger.team_calls[team]

    s = admin.ok_json("GET", f"/api/v1/dashboard/summary?team_id={team}")
    for f in ("total_cost_today", "total_cost_7d", "total_cost_30d", "total_cost_mtd"):
        results.decimal_equal(phase, f"{label}: overview {f} (traffic team)", exp_cost, s.get(f))
    for f in ("total_calls_today", "total_calls_mtd"):
        results.equal(phase, f"{label}: overview {f} (traffic team)", exp_calls, s.get(f))

    for gran in ("daily", "hourly"):
        pts = admin.ok_json("GET", f"/api/v1/dashboard/cost-over-time?days=2&granularity={gran}&team_id={team}")
        results.decimal_equal(phase, f"{label}: cost-over-time {gran} sum", exp_cost,
                              sum((_dec(p.get("cost")) for p in pts), Decimal("0")))

    rows = admin.ok_json("GET", f"/api/v1/dashboard/by-app?days=1&team_id={team}")
    for (t, app_id), c in sorted(ledger.app_cost.items()):
        if t != team:
            continue
        row = next((r for r in rows if app_id in (r.get("app_id"), r.get("app_name"))), None)
        results.decimal_equal(phase, f"{label}: by-app cost {app_id}", c, row.get("cost") if row else "missing")
        results.equal(phase, f"{label}: by-app calls {app_id}", ledger.app_calls[(t, app_id)],
                      (row.get("calls") if row else "missing"))

    models = admin.ok_json("GET", f"/api/v1/dashboard/top-models?days=1&team_id={team}")
    for (t, prov, model), c in sorted(ledger.model_cost.items()):
        if t != team:
            continue
        row = next((r for r in models if r.get("provider") == prov and r.get("model") == model), None)
        results.decimal_equal(phase, f"{label}: top-models cost {prov}/{model}", c, row.get("cost") if row else "missing")
        results.equal(phase, f"{label}: top-models calls {prov}/{model}", ledger.model_calls[(t, prov, model)],
                      row.get("calls") if row else "missing")

    fin = admin.ok_json("GET", "/api/v1/finance/summary")
    spender = next((x for x in fin.get("top_spenders", []) if x.get("team_slug") == tr.team_slug), None)
    results.decimal_equal(phase, f"{label}: finance top-spender spend (traffic team, cents)",
                          exp_cost.quantize(Decimal("0.01")), spender.get("spend_usd") if spender else "missing")

    if include_global:
        g = admin.ok_json("GET", "/api/v1/dashboard/summary")
        results.decimal_equal(phase, f"{label}: overview total_cost_today (all teams)", ledger.total, g.get("total_cost_today"))
        results.decimal_equal(phase, f"{label}: overview total_cost_mtd (all teams)", ledger.total, g.get("total_cost_mtd"))
        results.equal(phase, f"{label}: overview total_calls_today (all teams)", ledger.calls, g.get("total_calls_today"))
        results.decimal_equal(phase, f"{label}: finance total MTD spend (all teams, cents)",
                              ledger.total.quantize(Decimal("0.01")), fin.get("total_current_spend_usd"))


def reconcile_sessions(admin: Client, ledger: Ledger, tr: TrafficRun, results: Results,
                       run_label: str) -> None:
    """Attribution closes a session 120 s after its last call (loop every 30 s)."""
    phase = "reconcile"
    prefix = f"gate-{run_label}-"
    expected = {sid: v for sid, v in ledger.sessions.items() if sid.startswith(prefix)}
    wait_until_t = tr.sessions_done_at + 200
    got: dict[str, Any] = {}
    while True:
        rows = admin.ok_json("GET", "/api/v1/attribution/sessions?hours=24&limit=500")
        got = {x["session_id"]: x for x in rows if str(x.get("session_id", "")).startswith(prefix)}
        if len(got) >= len(expected) or time.time() > wait_until_t:
            break
        time.sleep(10)
    results.add(phase, "attribution: SDK sessions recorded", len(expected) > 0 and len(got) == len(expected),
                f"{len(expected)} sessions", f"{len(got)} sessions")
    bad = [sid for sid, (c, n) in expected.items()
           if sid not in got or _dec(got[sid].get("total_cost")) != c or got[sid].get("total_calls") != n]
    results.equal(phase, "attribution: sessions with exact cost and call count", [], bad[:3])
    results.decimal_equal(phase, "attribution: total session cost",
                          sum((c for c, _ in expected.values()), Decimal("0")),
                          sum((_dec(x.get("total_cost")) for x in got.values()), Decimal("0")))


def new_label() -> str:
    return uuid.uuid4().hex[:6]
