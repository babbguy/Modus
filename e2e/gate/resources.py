# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Resource budget: memory, idle CPU, and evaluate latency at concurrency 10."""
from __future__ import annotations

import http.client
import json
import statistics
import threading
import time
from urllib.parse import urlparse

from .common import Results, log
from .docker_env import StatsSampler

PHASE = "resources"
PEAK_MEMORY_LIMIT = 512 * 1024 * 1024
IDLE_CPU_LIMIT = 5.0


def evaluate_latency(base_url: str, app_key: str, concurrency: int = 10,
                     per_worker: int = 50) -> dict:
    """p50/p95/p99 of POST /api/v1/policy/evaluate with `concurrency` clients.

    500 requests in total, within the SDK rate-limit class's documented burst.
    """
    u = urlparse(base_url)
    lat: list[float] = []
    statuses: dict[int, int] = {}
    lock = threading.Lock()
    body = json.dumps({"provider": "openai", "model": "gpt-4o", "estimated_tokens": 1000}).encode()
    headers = {"Content-Type": "application/json", "X-Modus-APIKey": app_key}
    barrier = threading.Barrier(concurrency)
    # One request first, so the measurement is the steady state and not the
    # one-time bcrypt check of a key the server has not cached yet.
    warm = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    warm.request("POST", "/api/v1/policy/evaluate", body=body, headers=headers)
    warm.getresponse().read()
    warm.close()

    def worker() -> None:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        barrier.wait()
        for _ in range(per_worker):
            t0 = time.perf_counter()
            try:
                conn.request("POST", "/api/v1/policy/evaluate", body=body, headers=headers)
                resp = conn.getresponse()
                resp.read()
                code = resp.status
            except Exception:
                code = 0
                conn.close()
                conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
            dt = (time.perf_counter() - t0) * 1000
            with lock:
                lat.append(dt)
                statuses[code] = statuses.get(code, 0) + 1
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    lat.sort()

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(round(p / 100 * len(lat))) - 1)] if lat else 0.0
    return {"n": len(lat), "p50": pct(50), "p95": pct(95), "p99": pct(99),
            "mean": statistics.fmean(lat) if lat else 0.0, "rps": len(lat) / wall if wall else 0.0,
            "statuses": statuses}


def check_latency(base_url: str, app_key: str, results: Results) -> dict:
    lt = evaluate_latency(base_url, app_key)
    log(f"evaluate latency @10: p50={lt['p50']:.1f}ms p95={lt['p95']:.1f}ms p99={lt['p99']:.1f}ms "
        f"({lt['rps']:.0f} req/s) statuses={lt['statuses']}")
    results.metrics["evaluate p50 / p95 / p99 (concurrency 10)"] = (
        f"{lt['p50']:.1f} / {lt['p95']:.1f} / {lt['p99']:.1f} ms ({lt['n']} requests, {lt['rps']:.0f} req/s)")
    results.equal(PHASE, "evaluate benchmark (500 requests @ concurrency 10): non-200 responses", {},
                  {k: v for k, v in lt["statuses"].items() if k != 200})
    return lt


def measure_idle(stack, sampler: StatsSampler, warmup_s: float = 15, window_s: float = 60) -> float:
    """Average CPU (% of one core) over an idle window after a warm-up,
    from the container's cgroup CPU counter at both ends of the window."""
    sampler.phase = "idle-warmup"
    time.sleep(warmup_s)
    sampler.phase = "idle"
    u0, t0 = stack.cpu_usage_seconds(), time.monotonic()
    time.sleep(window_s)
    u1, t1 = stack.cpu_usage_seconds(), time.monotonic()
    sampler.phase = "after-idle"
    if u0 is None or u1 is None:   # no readable cgroup: fall back to docker stats
        vals = sampler.cpu_in("idle")
        return statistics.fmean(vals) if vals else float("nan")
    return (u1 - u0) / (t1 - t0) * 100


def check_resources(sampler: StatsSampler, idle_cpu: float, results: Results) -> None:
    peak = sampler.peak_mem()
    results.metrics["peak memory"] = f"{peak / 1024 / 1024:.0f} MiB (limit 512 MiB, container cap 1 GiB)"
    results.metrics["idle CPU (avg after warm-up)"] = f"{idle_cpu:.2f} % of one core over 60 s (limit 5 %)"
    results.add(PHASE, "peak memory <= 512 MiB", 0 < peak <= PEAK_MEMORY_LIMIT, "<= 512 MiB", f"{peak / 1024 / 1024:.0f} MiB")
    results.add(PHASE, "idle CPU average <= 5 %", idle_cpu == idle_cpu and idle_cpu <= IDLE_CPU_LIMIT,
                "<= 5 %", f"{idle_cpu:.2f} %")
