#!/usr/bin/env python3
# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Modus release gate: one command, real product, PASS or FAIL.

    python e2e/run_gate.py --db sqlite
    python e2e/run_gate.py --db postgres --image modus:local

Starts the production Docker image (capped at 1 CPU / 1 GiB) on SQLite or
PostgreSQL 16, drives it with the repository's own SDK and a real browser, and
fails on stale reads, miscounted usage, broken enforcement, server errors,
dashboard errors and resource overruns. See e2e/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

E2E = Path(__file__).resolve().parent
REPO = E2E.parent
sys.path.insert(0, str(E2E))

from gate import browser, logscan, resources  # noqa: E402
from gate.common import Client, GateError, Pacer, Results, log, make_jwt  # noqa: E402
from gate.docker_env import Stack, StatsSampler, build_image  # noqa: E402

# The gate's own admin traffic stays under the orchestrator's default rate
# limit (200/min + 50 burst per key or client IP), with headroom.
ADMIN_PACE_PER_MINUTE = 150


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", choices=["sqlite", "postgres"], required=True)
    p.add_argument("--image", default="", help="image to test; default: build the repository's Dockerfile")
    p.add_argument("--sdk", default=str(REPO / "sdk"),
                   help="SDK source directory to drive traffic with (default: this repository's sdk/)")
    p.add_argument("--out", default="", help="artifact directory (default: e2e/out/<db>)")
    p.add_argument("--chromium", default=os.environ.get("GATE_CHROMIUM", ""),
                   help="Chromium executable for the sweep (default: Playwright's own)")
    p.add_argument("--skip-browser", action="store_true", help="skip the dashboard sweep (local debugging only)")
    p.add_argument("--keep", action="store_true", help="leave the containers running (local debugging only)")
    return p.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles
    except Exception:
        pass
    args = parse_args()
    out_dir = Path(args.out or E2E / "out" / args.db).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in ("sweep-results.json", "gate-results.json", "summary.md"):
        (out_dir / old).unlink(missing_ok=True)

    sys.path.insert(0, str(Path(args.sdk).resolve()))
    for k in ("MODUS_URL", "MODUS_ORCHESTRATOR_URL", "MODUS_TEAM_TOKEN", "MODUS_APP_ID"):
        os.environ.pop(k, None)

    from gate import scenarios as sc
    from gate import traffic as tf
    from gate.write_read import run_write_then_read
    tf.install_sdk_logging()

    image = args.image
    if not image:
        image = "modus:gate"
        build_image(REPO, image)

    results = Results()
    results.metrics["image"] = image
    results.metrics["database"] = args.db
    stack = Stack(image=image, db=args.db, out_dir=out_dir)
    sampler = None
    t_start = time.time()
    try:
        stack.up(E2E / "gate" / "webhook_receiver.py")
        sampler = StatsSampler(stack.app)
        sampler.start()

        admin_jwt = make_jwt(stack.jwt_secret, sub="gate-admin")
        admin = Client(stack.base_url, {"Authorization": f"Bearer {admin_jwt}"}, Pacer(ADMIN_PACE_PER_MINUTE))
        master = Client(stack.base_url, {"X-Modus-APIKey": stack.master_key}, Pacer(ADMIN_PACE_PER_MINUTE))
        app_clients: list[Client] = []

        def app_client(key: str) -> Client:
            c = Client(stack.base_url, {"X-Modus-APIKey": key})
            app_clients.append(c)
            return c

        label = tf.new_label()
        ledger = tf.Ledger()
        before = logscan.metrics_status_counts(master)
        results.add("bring-up", f"Modus healthy on {args.db} (production mode, JWT auth)", True, "", stack.base_url)

        def step(phase: str, name: str, fn, *a, **kw):
            """Run one part of the gate; a crash is a failed check, and the
            remaining parts still run so every finding is reported."""
            try:
                return fn(*a, **kw)
            except Exception as exc:
                if not isinstance(exc, GateError):
                    traceback.print_exc()
                results.error(phase, f"{name} could not complete", exc)
                return None

        # 1. Write-then-read
        sampler.phase = "write-read"
        step("write-then-read", "write-then-read", run_write_then_read, admin, master, app_client, results, label)

        # 2. Traffic through the real SDK, then an immediate reconciliation
        sampler.phase = "traffic"
        tr = step("traffic", "SDK traffic", tf.run_traffic, stack, admin, master, ledger, results, label)
        if tr is not None:
            step("traffic", "SDK health checks", tf.sdk_health_checks, tr, results)
            step("reconcile", "visibility check", tf.await_visible, admin, ledger, tr, results)
            step("reconcile", "reconciliation right after traffic", tf.reconcile, admin, ledger, tr, results,
                 "right after traffic", include_global=False)

        # 3. Enforcement and chaos
        sampler.phase = "scenarios"
        s = sc.Scenarios(stack, admin, master, app_client, ledger, results, label)
        step(sc.PHASE, "chaos setup", s.setup)
        if s.apps:
            for fn in (s.budget_cap, s.rate_limit, s.denylist_server, s.ladder, s.alerts, s.connections,
                       s.evaluate_timeout):
                step(sc.PHASE, fn.__name__, fn)
        last_write = time.time()

        # 4. Latency of the enforcement hot path
        sampler.phase = "benchmark"

        def benchmark() -> None:
            bench = master.ok_json("POST", "/api/v1/apps/register",
                                   {"app_id": f"gate-bench-{label}", "app_name": f"gate-bench-{label}",
                                    "team_slug": f"gate-chaos-{label}"})
            time.sleep(1)
            resources.check_latency(stack.base_url, bench["api_key"], results)
        step("resources", "evaluate benchmark", benchmark)

        # 5. Still exact after several aggregator cycles (interval 10 s)
        sampler.phase = "reconcile"
        if tr is not None:
            wait = max(0.0, last_write + 45 - time.time())
            log(f"waiting {wait:.0f}s so at least 4 aggregator cycles run after the last write")
            time.sleep(wait)
            step("reconcile", "reconciliation after aggregator cycles", tf.reconcile, admin, ledger, tr, results,
                 "after 4+ aggregator cycles", include_global=True)
            step("reconcile", "session reconciliation", tf.reconcile_sessions, admin, ledger, tr, results, label)
            step("reconcile", "final reconciliation", tf.reconcile, admin, ledger, tr, results,
                 "at the end of the run", include_global=True)
            for a in tr.apps:
                if a.api_key:
                    a.stop()
        s.stop()

        # 6. Dashboard sweep
        sampler.phase = "browser"
        if not args.skip_browser:
            kpis = {"Cost 7D": browser.fmt_cost(ledger.total), "Cost 30D": browser.fmt_cost(ledger.total)}
            step("browser", "browser sweep", browser.run_sweep, stack.base_url, stack.master_key, out_dir, kpis,
                 {"traffic_team": tr.team_slug if tr else "", "label": label}, results, args.chromium)

        # 7. Idle CPU
        idle = resources.measure_idle(stack, sampler)

        # 8. Server-side status codes, logs, resources
        after = logscan.metrics_status_counts(master)
        delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
        results.equal("server-logs", "HTTP 429 responses served during the run (all clients)", 0, int(delta.get("429", 0)))
        results.equal("server-logs", "HTTP 5xx responses served during the run", [], logscan.metrics_5xx_paths(master))
        client_429 = admin.rate_limited + master.rate_limited + [e for c in app_clients for e in c.rate_limited]
        client_429 += [f"SDK: {m}" for p, _, m in tf.SDK_LOG.records if "429" in m]
        results.equal("server-logs", "HTTP 429 responses seen by the gate's clients and SDK agents", [], client_429[:4])
        client_5xx = admin.server_errors + master.server_errors + [e for c in app_clients for e in c.server_errors]
        results.equal("server-logs", "5xx responses seen by the gate's clients", [], client_5xx[:4])
        sampler.stop()
        resources.check_resources(sampler, idle, results)
        logscan.check_logs(stack.logs(), results)
    except GateError as exc:
        results.error("harness", "gate run", exc)
    except Exception as exc:
        traceback.print_exc()
        results.error("harness", "gate run", exc)
    finally:
        if sampler:
            sampler.stop()
            with open(out_dir / "docker-stats.csv", "w", encoding="utf-8") as f:
                f.write("t_s,cpu_pct,mem_mib,phase\n")
                t0 = sampler.samples[0][0] if sampler.samples else 0
                for t, cpu, mem, ph in sampler.samples:
                    f.write(f"{t - t0:.1f},{cpu:.2f},{mem / 1048576:.1f},{ph}\n")
        with open(out_dir / "sdk.log", "w", encoding="utf-8") as f:
            for phase, level, msg in tf.SDK_LOG.records:
                f.write(f"{phase}\t{level}\t{msg}\n")
        if args.keep:
            log(f"--keep: containers left running ({stack.prefix}-*), base URL {stack.base_url}")
        else:
            stack.down()
    results.metrics["duration"] = f"{time.time() - t_start:.0f} s"
    return report(results, out_dir, args.db)


def report(results: Results, out_dir: Path, db: str) -> int:
    passed = not results.failed
    rows = [(c.phase, c.name, "PASS" if c.ok else "FAIL", c.expected if not c.ok else "", c.observed if not c.ok else "")
            for c in results.checks]
    w0 = max([len(r[0]) for r in rows] + [5])
    w1 = min(max([len(r[1]) for r in rows] + [5]), 90)
    print()
    print(f"{'PHASE'.ljust(w0)}  {'CHECK'.ljust(w1)}  RESULT")
    print(f"{'-' * w0}  {'-' * w1}  ------")
    for ph, name, res, exp, obs in rows:
        print(f"{ph.ljust(w0)}  {name[:w1].ljust(w1)}  {res}")
        if res == "FAIL":
            print(f"{' ' * w0}    expected: {exp}")
            print(f"{' ' * w0}    observed: {obs}")
    print()
    for k, v in results.metrics.items():
        print(f"{k}: {v}")
    verdict = "PASS" if passed else f"FAIL ({len(results.failed)} of {len(results.checks)} checks failed)"
    print(f"\nRELEASE GATE ({db}): {verdict}\n")

    md = [f"## Release gate ({db}): {'PASS' if passed else 'FAIL'}", "",
          f"{len(results.checks) - len(results.failed)} of {len(results.checks)} checks passed.", "",
          "| Phase | Check | Result | Expected | Observed |", "|---|---|---|---|---|"]
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    for ph, name, res, exp, obs in rows:
        md.append(f"| {esc(ph)} | {esc(name)} | {'PASS' if res == 'PASS' else '**FAIL**'} | {esc(exp)} | {esc(obs)} |")
    md += ["", "| Measurement | Value |", "|---|---|"] + [f"| {esc(k)} | {esc(v)} |" for k, v in results.metrics.items()]
    text = "\n".join(md) + "\n"
    (out_dir / "summary.md").write_text(text, encoding="utf-8")
    (out_dir / "gate-results.json").write_text(json.dumps({
        "passed": passed, "db": db, "metrics": results.metrics,
        "checks": [c.__dict__ for c in results.checks]}, indent=1), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
