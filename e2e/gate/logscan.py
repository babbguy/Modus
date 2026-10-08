# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Server log and metrics check.

After the run, every line the Modus container wrote is scanned. Any ERROR or
CRITICAL record, Python traceback, unhandled exception, PendingRollbackError
or 5xx response fails the gate, unless it matches the allow-list below.
"""
from __future__ import annotations

import json
import re

from .common import Client, Results

PHASE = "server-logs"

# Messages that are expected during a gate run. Every entry must say why.
# Keep this list short: anything added here is a failure the gate stops seeing.
ALLOWED: list[tuple[str, str]] = [
    # (regex on the message, reason it is expected)
]

PATTERNS = [
    ("Traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("PendingRollbackError", re.compile(r"PendingRollbackError")),
    ("unhandled exception", re.compile(r"[Uu]nhandled exception")),
]


def _allowed(text: str) -> bool:
    return any(re.search(rx, text) for rx, _ in ALLOWED)


def scan_logs(text: str) -> list[str]:
    findings: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        level, message = "", line
        if line.startswith("{"):
            try:
                rec = json.loads(line)
                level = str(rec.get("level", "")).upper()
                message = f"{rec.get('logger', '')}: {rec.get('message', '')}"
                if rec.get("exception"):
                    message += " | " + str(rec["exception"]).strip().splitlines()[-1]
                    if "Traceback" in str(rec["exception"]):
                        level = level or "ERROR"
            except ValueError:
                pass
        else:
            m = re.search(r"\b(ERROR|CRITICAL)\b", line)
            if m and re.match(r"^(\S+\s+)?\S*\s*(ERROR|CRITICAL)", line):
                level = m.group(1)
        hit = None
        if level in ("ERROR", "CRITICAL"):
            hit = f"{level} {message}"
        else:
            for name, rx in PATTERNS:
                if rx.search(line):
                    hit = f"{name}: {message}"
                    break
        if hit and not _allowed(hit):
            findings.append(hit[:400])
    return findings


def metrics_status_counts(client: Client) -> dict[str, float]:
    """Sum modus_http_requests_total by status code from /metrics."""
    r = client.get("/metrics", paced=False)
    counts: dict[str, float] = {}
    if not r.ok or not isinstance(r.body, str):
        return counts
    for line in r.body.splitlines():
        if line.startswith("modus_http_requests_total{"):
            m = re.search(r'status_code="(\d+)"', line)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + float(line.rsplit(" ", 1)[1])
    return counts


def metrics_5xx_paths(client: Client) -> list[str]:
    r = client.get("/metrics", paced=False)
    out = []
    if r.ok and isinstance(r.body, str):
        for line in r.body.splitlines():
            if line.startswith("modus_http_requests_total{") and re.search(r'status_code="5\d\d"', line):
                if float(line.rsplit(" ", 1)[1]) > 0:
                    out.append(line.split("{", 1)[1].rsplit("}", 1)[0])
    return out


def check_logs(text: str, results: Results) -> list[str]:
    findings = scan_logs(text)
    uniq: dict[str, int] = {}
    for f in findings:
        key = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<id>", f)
        uniq[key] = uniq.get(key, 0) + 1
    shown = [f"{n}x {k}" for k, n in sorted(uniq.items(), key=lambda kv: -kv[1])]
    results.equal(PHASE, "ERROR lines, tracebacks, unhandled exceptions, PendingRollbackError", [], shown[:6]
                  + ([f"... {len(shown) - 6} more distinct"] if len(shown) > 6 else []))
    return shown
