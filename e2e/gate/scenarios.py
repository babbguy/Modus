# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Enforcement and chaos scenarios, each driven through the real product.

Every scenario uses its own app in a dedicated chaos team, so the traffic
team's figures stay exact. Usage recorded here still goes into the gate's
ledger and is part of the all-teams reconciliation.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable, Optional

from .common import Client, Results, log, wait_until
from .traffic import SDK_LOG, Ledger, SdkApp, register_concurrently

PHASE = "scenarios"
VISIBLE_WITHIN_S = 5.0   # spend must reach enforcement this fast after the SDK flush


def _violation(fn: Callable[[], object]):
    """Run an SDK enforce call; return the PolicyViolationError or None."""
    from modus.agent import PolicyViolationError
    try:
        fn()
        return None
    except PolicyViolationError as e:
        return e


def _spend(a: SdkApp, cost: str, model: str = "gpt-4o") -> None:
    """Record one call of an exact cost and push it to the server now."""
    c = Decimal(cost)
    a.agent.record(provider="openai", model=model, input_tokens=500, output_tokens=50,
                   input_cost=c, output_cost=Decimal("0"), duration_ms=120)
    a.ledger.add(a.team_id, a.app_id, "openai", model, c)
    a.flush()


class Scenarios:
    def __init__(self, stack, admin: Client, master: Client, app_client: Callable[[str], Client],
                 ledger: Ledger, results: Results, label: str):
        self.stack = stack
        self.admin = admin
        self.master = master
        self.app_client = app_client
        self.ledger = ledger
        self.r = results
        self.label = label
        self.team_id = ""
        self.apps: dict[str, SdkApp] = {}

    # ── setup ───────────────────────────────────────────────────────────────
    def setup(self) -> None:
        SDK_LOG.phase = PHASE
        slug = f"gate-chaos-{self.label}"
        self.team_id = self.admin.ok_json("POST", "/api/v1/teams", {"slug": slug, "name": f"Gate chaos {self.label}"})["id"]
        token = self.master.ok_json("POST", f"/api/v1/teams/{self.team_id}/registration-token", {})["registration_token"]
        names = ["budget", "ratelimit", "denylist", "ladder", "alert"]
        apps = [SdkApp(self.stack.base_url, token, self.team_id, f"gate-{n}-{self.label}", self.ledger, True, 7,
                       flush_interval=3600) for n in names]
        failed = register_concurrently(apps)
        self.r.equal(PHASE, "chaos apps registered concurrently: failures", [], failed)
        self.apps = dict(zip(names, apps))

    def _policy(self, body: dict) -> Optional[dict]:
        r = self.admin.post("/api/v1/policies", body)
        if not self.r.add(PHASE, f"create policy '{body['name']}'", r.status in (200, 201), "201", r):
            return None
        return r.body

    def _await_decision(self, a: SdkApp, model: str, want: str, timeout_s: float = VISIBLE_WITHIN_S) -> dict:
        """Ingest is written through the server's write queue (flushed every
        0.25 s), so enforcement may lag a flush slightly. It must catch up
        within VISIBLE_WITHIN_S seconds."""
        deadline = time.monotonic() + timeout_s
        while True:
            raw = self._raw_evaluate(a, model)
            if raw.get("decision") == want or time.monotonic() > deadline:
                return raw
            time.sleep(0.25)

    def _await_decision_with(self, a: SdkApp, model: str, pred) -> dict:
        deadline = time.monotonic() + VISIBLE_WITHIN_S
        while True:
            raw = self._raw_evaluate(a, model)
            if pred(raw) or time.monotonic() > deadline:
                return raw
            time.sleep(0.25)

    def _raw_evaluate(self, a: SdkApp, model: str) -> dict:
        r = self.app_client(a.api_key).post("/api/v1/policy/evaluate", {"provider": "openai", "model": model}, paced=False)
        return r.body if r.ok and isinstance(r.body, dict) else {"decision": f"HTTP {r.status}", "body": r.body}

    # ── budget cap ──────────────────────────────────────────────────────────
    def budget_cap(self) -> None:
        a = self.apps["budget"]
        if not a.api_key:
            return
        if not self._policy({"name": f"gate budget cap {self.label}", "scope": "app", "team_id": self.team_id,
                             "app_id": a.agent._app_uuid, "policy_type": "budget_cap", "effect": "deny",
                             "config": {"cap_usd": "0.01", "period": "daily"}}):
            return
        e0 = _violation(lambda: a.agent.enforce(provider="openai", model="gpt-4o", bypass_cache=True))
        self.r.add(PHASE, "budget cap: allowed with no spend", e0 is None, "allow", e0)
        _spend(a, "0.004")
        e1 = _violation(lambda: a.agent.enforce(provider="openai", model="gpt-4o", bypass_cache=True))
        self.r.add(PHASE, "budget cap: allowed at $0.004 of $0.01", e1 is None, "allow", e1)
        _spend(a, "0.0065")
        raw = self._await_decision(a, "gpt-4o", "deny")
        self.r.add(PHASE, f"budget cap: server evaluate denies within {VISIBLE_WITHIN_S:.0f} s of the spend",
                   raw.get("decision") == "deny" and "budget" in str(raw.get("reason")).lower(), "deny (budget cap)", raw)
        e2 = _violation(lambda: a.agent.enforce(provider="openai", model="gpt-4o", bypass_cache=True))
        self.r.add(PHASE, "budget cap: SDK enforce denied at $0.0105 of $0.01",
                   e2 is not None and e2.decision == "deny" and "budget" in str(e2.reason).lower(),
                   "deny (budget cap)", e2 and f"{e2.decision}: {e2.reason}")

    # ── rate-limit policy ───────────────────────────────────────────────────
    def rate_limit(self) -> None:
        a = self.apps["ratelimit"]
        if not a.api_key:
            return
        if not self._policy({"name": f"gate rate limit {self.label}", "scope": "app", "team_id": self.team_id,
                             "app_id": a.agent._app_uuid, "policy_type": "rate_limit", "effect": "throttle",
                             "config": {"max_calls": 5, "window_seconds": 3600},
                             "action": {"retry_after_seconds": 42}}):
            return
        for _ in range(4):
            _spend(a, "0.00012345")
        e0 = _violation(lambda: a.agent.enforce(provider="openai", model="gpt-4o", bypass_cache=True))
        self.r.add(PHASE, "rate limit: call 5 of 5 allowed", e0 is None, "allow", e0)
        _spend(a, "0.00012345")
        raw = self._await_decision(a, "gpt-4o", "throttle")
        self.r.add(PHASE, f"rate limit: server evaluate throttles within {VISIBLE_WITHIN_S:.0f} s, retry_after_seconds=42",
                   raw.get("decision") == "throttle" and raw.get("retry_after_seconds") == 42,
                   "throttle / 42", raw)
        e1 = _violation(lambda: a.agent.enforce(provider="openai", model="gpt-4o", bypass_cache=True))
        self.r.add(PHASE, "rate limit: SDK enforce throttles call 6 with Retry-After 42 s",
                   e1 is not None and e1.decision == "throttle" and getattr(e1, "retry_after_seconds", None) == 42,
                   "throttle, retry_after_seconds=42",
                   e1 and f"{e1.decision}, retry_after_seconds={getattr(e1, 'retry_after_seconds', None)}")

    # ── model denylist (server path; the local path is checked in traffic) ──
    def denylist_server(self) -> None:
        a = self.apps["denylist"]
        if not a.api_key:
            return
        model = f"gate-late-denied-{self.label}"
        # Created after the SDK synced its policies: only the server knows it.
        if not self._policy({"name": f"gate late denylist {self.label}", "scope": "team", "team_id": self.team_id,
                             "policy_type": "model_denylist", "effect": "deny", "config": {"models": [model]}}):
            return
        e = _violation(lambda: a.agent.enforce(provider="openai", model=model, bypass_cache=True))
        self.r.add(PHASE, "denylist: SDK call denied by the server (policy unknown locally)",
                   e is not None and e.decision == "deny" and "local policy" not in str(e.reason),
                   "deny from server", e and f"{e.decision}: {e.reason}")
        raw = self._raw_evaluate(a, model)
        self.r.add(PHASE, "denylist: server evaluate denies", raw.get("decision") == "deny", "deny", raw)
        ok = self._raw_evaluate(a, "gpt-4o")
        self.r.add(PHASE, "denylist: other models still allowed", ok.get("decision") == "allow", "allow", ok)

    # ── degradation ladder never bypasses a deny ────────────────────────────
    def ladder(self) -> None:
        a = self.apps["ladder"]
        if not a.api_key:
            return
        denied = f"gate-ladder-denied-{self.label}"
        if not self._policy({"name": f"gate ladder {self.label}", "scope": "app", "team_id": self.team_id,
                             "app_id": a.agent._app_uuid, "policy_type": "degradation_ladder", "effect": "deny",
                             "config": {"budget_usd": "0.01", "period": "daily",
                                        "tiers": [{"pct": 50, "model": "gpt-4o-mini"}, {"pct": 100, "action": "deny"}]}}):
            return
        if not self._policy({"name": f"gate ladder deny {self.label}", "scope": "team", "team_id": self.team_id,
                             "policy_type": "model_denylist", "effect": "deny", "config": {"models": [denied]}}):
            return
        _spend(a, "0.006")    # 60 % of the ladder budget: downshift tier
        raw = self._await_decision_with(a, "gpt-4o", lambda d: d.get("suggested_model") == "gpt-4o-mini")
        self.r.add(PHASE, "ladder: 60% of budget -> allow with downshift to gpt-4o-mini",
                   raw.get("decision") == "allow" and raw.get("suggested_model") == "gpt-4o-mini",
                   "allow + gpt-4o-mini", raw)
        sdk_suggested = None
        e = _violation(lambda: None)
        try:
            sdk_suggested = a.agent.enforce(provider="openai", model="gpt-4o", bypass_cache=True)
        except Exception as exc:   # PolicyViolationError would be wrong here
            e = exc
        self.r.add(PHASE, "ladder: SDK enforce returns the downshift model", e is None and sdk_suggested == "gpt-4o-mini",
                   "gpt-4o-mini", sdk_suggested if e is None else repr(e))
        raw_d = self._raw_evaluate(a, denied)
        self.r.add(PHASE, "ladder: a denied model stays denied while the ladder is active",
                   raw_d.get("decision") == "deny", "deny", raw_d)
        _spend(a, "0.0045")   # 105 %: the ladder's own deny tier
        raw_full = self._await_decision(a, "gpt-4o-mini", "deny")
        self.r.add(PHASE, "ladder: budget exhausted -> deny even on the downshift model",
                   raw_full.get("decision") == "deny", "deny", raw_full)

    # ── alert threshold -> webhook delivery ─────────────────────────────────
    def alerts(self) -> None:
        a = self.apps["alert"]
        if not a.api_key:
            return
        cfg = {"webhook": {"enabled": True, "url": self.stack.hook_url_app + "/alerts",
                           "secret_header": "X-Gate-Secret", "secret_value": "gate-" + self.label,
                           "min_severity": "warning"}}
        r = self.admin.put("/api/v1/notifications/config", cfg)
        self.r.add(PHASE, "alerts: webhook channel configured", r.ok, "200", r)
        t = self.admin.post("/api/v1/thresholds", {
            "name": f"gate alert {self.label}", "team_id": self.team_id, "app_id": a.agent._app_uuid,
            "scope": "app", "metric": "total_cost", "period": "daily", "critical_value": "0.01",
        })
        if not self.r.add(PHASE, "alerts: threshold created", t.status == 201, "201", t):
            return
        threshold_id = t.body["id"]
        _spend(a, "0.0123")   # 123 % of the threshold
        t0 = time.time()

        def delivered():
            got = self.hook_received()
            return [g for g in got if isinstance(g.get("payload"), dict)
                    and g["payload"].get("threshold_id") == threshold_id]
        hits = wait_until(delivered, 60, 2) or []
        sev = sorted({h["payload"].get("severity") for h in hits})
        self.r.add(PHASE, "alerts: critical alert delivered to the webhook",
                   "critical" in sev, "critical delivered", f"{sev} after {time.time() - t0:.0f}s")
        if hits:
            p = hits[-1]["payload"]
            self.r.decimal_equal(PHASE, "alerts: webhook payload actual_value is the exact spend",
                                 Decimal("0.0123"), p.get("actual_value"))

        # The channel's min_severity is "warning": warning and critical alerts
        # must be delivered and recorded as such; the 70 % "caution" tier is
        # below the channel minimum and must not be sent.
        def mine() -> list:
            rows = self.admin.get("/api/v1/dashboard/recent-alerts?limit=100").body or []
            return [x for x in rows if isinstance(x, dict) and x.get("app_id") == a.agent._app_uuid]

        def recorded():
            rows = [x for x in mine() if x.get("severity") in ("warning", "critical")]
            return rows if len(rows) >= 2 and all(x.get("notification_sent") for x in rows) else None
        wait_until(recorded, 30, 3)
        rows = mine()
        sendable = [x for x in rows if x.get("severity") in ("warning", "critical")]
        status = lambda x: ((x.get("notification_result") or {}).get("webhook") or {}).get("status")  # noqa: E731
        summary = [(x.get("severity"), x.get("notification_sent"), status(x)) for x in rows] or "no alerts"
        self.r.add(PHASE, "alerts: warning and critical alerts recorded as delivered (notification_sent, webhook=delivered)",
                   {x.get("severity") for x in sendable} == {"warning", "critical"}
                   and all(x.get("notification_sent") is True and status(x) == "delivered" for x in sendable),
                   "warning+critical: notification_sent=True, webhook=delivered", summary)
        below = [x for x in rows if x.get("severity") not in ("warning", "critical")]
        self.r.add(PHASE, "alerts: alerts below the channel's min_severity are not sent",
                   all(x.get("notification_sent") is not True and status(x) is None for x in below),
                   "not sent", summary)

    def hook_received(self) -> list:
        c = Client(self.stack.hook_url_host)
        r = c.get("/_gate/received")
        return r.body if r.ok and isinstance(r.body, list) else []

    def hook_mode(self, mode: str) -> None:
        Client(self.stack.hook_url_host).post("/_gate/mode", {"mode": mode})

    # ── connection health ───────────────────────────────────────────────────
    def connections(self) -> None:
        def test() -> dict:
            r = self.admin.post("/api/v1/admin/connections/notify_custom/test", {}, timeout=20)
            return r.body if r.ok and isinstance(r.body, dict) else {"status": f"HTTP {r.status}", "body": r.body}

        self.hook_mode("ok")
        ok = test()
        self.r.equal(PHASE, "connections: webhook healthy -> connected", "connected", ok.get("status"))
        self.hook_mode("slow")
        slow = test()
        self.r.add(PHASE, "connections: slow webhook (2.2 s) -> degraded", slow.get("status") == "degraded",
                   "degraded", f"{slow.get('status')} ({slow.get('last_error')})")
        self.hook_mode("fail")
        fail = test()
        self.r.add(PHASE, "connections: webhook answering 503 -> degraded", fail.get("status") == "degraded",
                   "degraded", f"{fail.get('status')} ({fail.get('last_error')})")
        self.hook_mode("ok")
        # Point the channel at a closed port: unreachable -> error.
        cfg = self.admin.get("/api/v1/notifications/config").body or {}
        good_url = self.stack.hook_url_app + "/alerts"
        broken = dict(cfg.get("webhook") or {}, url=f"http://{self.stack.hook}:9/alerts")
        self.admin.put("/api/v1/notifications/config", {**cfg, "webhook": broken})
        err = test()
        self.r.add(PHASE, "connections: unreachable webhook -> error", err.get("status") == "error",
                   "error", f"{err.get('status')} ({err.get('last_error')})")
        listing = self.admin.get("/api/v1/admin/connections")
        summary = (listing.body or {}).get("summary", {}) if listing.ok else {}
        self.r.add(PHASE, "connections: list summary counts the error", summary.get("error", 0) >= 1,
                   "error >= 1", summary or listing)
        self.admin.put("/api/v1/notifications/config", {**cfg, "webhook": dict(broken, url=good_url)})
        test()

    # ── evaluate timeout under a database lock ──────────────────────────────
    def evaluate_timeout(self) -> None:
        """/api/v1/evaluate/ must answer with its fail-closed decision when the
        evaluation overruns MODUS_ENFORCEMENT_TIMEOUT_MS (500 ms), never a 5xx.

        PostgreSQL only: another session holds an exclusive lock on `apps` for
        4 s (a migration or maintenance query), so the evaluation waits on I/O
        past its budget -- a deterministic stall.

        SQLite has no equivalent: WAL never blocks readers, an exclusive
        locking mode cannot be taken while the server's pooled connections are
        open, and CPU starvation is not deterministic (the event loop only
        checks its timers when it gets CPU, so a starved evaluation often
        completes in the same slice and is allowed). Rather than report a
        result that depends on runner speed, the SQLite job records no check
        here. The behaviour is covered by the PostgreSQL job and by
        tests/test_commit_before_response.py (timeout and error fallbacks).
        """
        if self.stack.db != "postgres":
            log("evaluate timeout: not run on SQLite (no deterministic I/O stall); "
                "covered by the PostgreSQL job and tests/test_commit_before_response.py")
            return
        a = self.apps["denylist"]
        if not a.api_key:
            return
        stall = self._stall(seconds=4)
        if stall is None:
            self.r.add(PHASE, "evaluate timeout: stall injected", False, "stall", "could not inject the stall")
            return
        what, wait_release = stall
        body = {"provider": "openai", "model": "gpt-4o", "input_tokens": 1000, "app_id": a.agent._app_id}
        r = self.admin.post("/api/v1/evaluate/", body, timeout=60)
        wait_release()
        self.r.add(PHASE, f"evaluate timeout ({what}): never a 5xx", r.status < 500, "< 500", r.status)
        decision = r.body.get("decision") if isinstance(r.body, dict) else None
        reason = str(r.body.get("reason")) if isinstance(r.body, dict) else str(r.body)
        self.r.add(PHASE, f"evaluate timeout ({what}): fail-closed deny", r.status == 200 and decision == "deny"
                   and "timed out" in reason and "fail-closed" in reason,
                   "200 deny 'timed out ... fail-closed'", f"{r.status} {decision}: {reason[:120]}")
        after = self.admin.post("/api/v1/evaluate/", body, timeout=20)
        self.r.add(PHASE, f"evaluate timeout ({what}): next evaluate after the stall allows",
                   after.status == 200 and isinstance(after.body, dict) and after.body.get("decision") == "allow",
                   "200 allow", after)

    def _stall(self, seconds: int):
        if self.stack.db == "postgres":
            p = self.stack.popen_exec(self.stack.pg, "psql", "-v", "ON_ERROR_STOP=1", "-U", "modus", "-d", "modus", "-c",
                                      f"BEGIN; LOCK TABLE apps IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep({seconds}); COMMIT;")
            q = "SELECT count(*) FROM pg_locks WHERE relation = 'apps'::regclass AND mode = 'AccessExclusiveLock' AND granted"
            held = wait_until(lambda: self.stack.exec(self.stack.pg, "psql", "-tA", "-U", "modus", "-d", "modus",
                                                      "-c", q).stdout.strip() == "1", 10, 0.2)
            if not held:
                p.kill()
                return None
            return "database lock", lambda: p.wait(timeout=60)
        return None  # only PostgreSQL offers a deterministic stall (see evaluate_timeout)

    def stop(self) -> None:
        for a in self.apps.values():
            if a.api_key:
                a.stop()
