#!/usr/bin/env python3
"""
Modus — Gateway / Evaluate Latency Benchmark
===============================================
Honestly measures Modus's *own* overhead, isolated from provider variance,
so a p99 claim is reproducible rather than assumed.

Two modes:

  evaluate  — drive POST /api/v1/evaluate at a target concurrency and report
              p50/p95/p99 of the enforcement decision latency. This is the
              number the README's "warm path" refers to. Requires the
              orchestrator running with realistic seeded spend/threshold/policy
              rows (an empty DB is not a meaningful benchmark).

  gateway   — drive the proxy against a MOCK upstream (a fixed-latency echo) so
              the measured latency minus the mock's fixed delay is Modus's
              proxy overhead, with zero provider noise. Start the mock with
              `--serve-mock` in another terminal.

This script is stdlib-only (urllib + threading) so it runs anywhere the
orchestrator does, including an air-gapped box.

Examples:
    # terminal 1: a 40ms fixed-latency mock OpenAI upstream
    python scripts/benchmark_gateway.py --serve-mock --mock-delay-ms 40 --mock-port 9099

    # terminal 2: 100 evaluate requests at concurrency 10
    python scripts/benchmark_gateway.py evaluate --base http://localhost:8080 \
        --api-key mds_... --app-id my-app --n 100 --concurrency 10

Report the resulting table in docs/benchmarking.md with the machine spec
(target: 1 vCPU / 1 GB — the $5 VPS class). Do not publish a p99 you have not
measured on that class of hardware.
"""
import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ── Mock upstream ─────────────────────────────────────────────────────────────

def serve_mock(port: int, delay_ms: float) -> None:
    """A fixed-latency echo standing in for a provider, to isolate overhead."""
    body = json.dumps({
        "id": "mock", "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop", "index": 0}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            time.sleep(delay_ms / 1000.0)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print(f"mock upstream on :{port} (fixed {delay_ms}ms delay) — Ctrl-C to stop")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ── Load driver ───────────────────────────────────────────────────────────────

def _post(url: str, payload: dict, headers: dict) -> float:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "content-type": "application/json", **headers,
    }, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        e.read()  # a policy deny (403/429) is still a valid, timed response
    return (time.perf_counter() - t0) * 1000.0  # ms


def run_load(url: str, payload: dict, headers: dict, n: int, concurrency: int) -> list[float]:
    latencies: list[float] = []
    lock = threading.Lock()

    def one(_):
        ms = _post(url, payload, headers)
        with lock:
            latencies.append(ms)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, range(n)))
    return latencies


def report(label: str, latencies: list[float], subtract_ms: float = 0.0) -> None:
    if not latencies:
        print("no samples")
        return
    adj = sorted(max(0.0, x - subtract_ms) for x in latencies)
    def pct(p):
        return adj[min(len(adj) - 1, int(len(adj) * p))]
    print(f"\n{label}  (n={len(adj)}"
          + (f", minus {subtract_ms}ms mock delay = overhead" if subtract_ms else "")
          + ")")
    print(f"  p50={pct(0.50):7.2f}ms  p95={pct(0.95):7.2f}ms  "
          f"p99={pct(0.99):7.2f}ms  max={adj[-1]:7.2f}ms  "
          f"mean={statistics.mean(adj):7.2f}ms")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serve-mock", action="store_true", help="run the mock upstream")
    ap.add_argument("--mock-delay-ms", type=float, default=40.0)
    ap.add_argument("--mock-port", type=int, default=9099)
    ap.add_argument("mode", nargs="?", choices=["evaluate", "gateway"])
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--app-id", default="bench-app")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=10)
    args = ap.parse_args(argv[1:])

    if args.serve_mock:
        serve_mock(args.mock_port, args.mock_delay_ms)
        return 0

    if args.mode == "evaluate":
        url = f"{args.base}/api/v1/evaluate/"
        payload = {"app_id": args.app_id, "provider": "openai", "model": "gpt-4o",
                   "input_tokens": 500, "output_tokens": 500}
        headers = {"X-Modus-APIKey": args.api_key} if args.api_key else {}
        lat = run_load(url, payload, headers, args.n, args.concurrency)
        report("evaluate (enforcement decision latency)", lat)
        return 0

    if args.mode == "gateway":
        url = f"{args.base}/gateway/openai/v1/chat/completions"
        payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        headers = {"X-Modus-APIKey": args.api_key} if args.api_key else {}
        lat = run_load(url, payload, headers, args.n, args.concurrency)
        report("gateway proxy total", lat)
        report("gateway proxy overhead", lat, subtract_ms=args.mock_delay_ms)
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
