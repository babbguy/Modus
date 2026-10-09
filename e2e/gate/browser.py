# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Runs the Playwright sweep (e2e/sweep/sweep.mjs) and turns its output into checks."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .common import Results, log

PHASE = "browser"
E2E = Path(__file__).resolve().parent.parent


def fmt_cost(v: Decimal) -> str:
    """Python twin of dashboard/js/format.js fmtCost."""
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 10_000:
        return f"{sign}${(a / 1000).quantize(Decimal('0.1'), ROUND_HALF_UP)}k"
    if 0 < a < 1:
        return f"{sign}${a.quantize(Decimal('0.0001'), ROUND_HALF_UP)}"
    return f"{sign}${a.quantize(Decimal('0.01'), ROUND_HALF_UP):,}"


def fmt_num(v: int) -> str:
    """Python twin of dashboard/js/format.js fmtNum."""
    if v >= 1_000_000:
        return f"{Decimal(v) / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{(Decimal(v) / 1000).quantize(Decimal('0.1'), ROUND_HALF_UP)}K"
    return str(v)


def run_sweep(base_url: str, master_key: str, out_dir: Path, kpis: dict, variables: dict,
              results: Results, chromium: str = "") -> None:
    node = shutil.which("node")
    if not node:
        results.add(PHASE, "browser sweep ran", False, "node on PATH", "node not found")
        return
    if not (E2E / "node_modules" / "playwright").exists():
        results.add(PHASE, "browser sweep ran", False, "npm ci in e2e/", "e2e/node_modules/playwright missing")
        return
    cfg = {"base": base_url, "masterKey": master_key, "outDir": str(out_dir),
           "expectations": str(E2E / "sweep" / "view_expectations.json"), "kpis": kpis, "vars": variables}
    if chromium:
        cfg["chromium"] = chromium
    cfg_path = out_dir / "sweep-config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    log("browser sweep: opening every dashboard view")
    try:
        proc = subprocess.run([node, str(E2E / "sweep" / "sweep.mjs"), str(cfg_path)], cwd=str(E2E),
                              capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace",
                              env={**os.environ})
    finally:
        cfg_path.unlink(missing_ok=True)   # holds the master key
    (out_dir / "sweep.log").write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
    res_path = out_dir / "sweep-results.json"
    if proc.returncode != 0 or not res_path.exists():
        results.add(PHASE, "browser sweep ran", False, "exit 0", f"exit {proc.returncode}: {(proc.stderr or '')[-300:]}")
        return
    data = json.loads(res_path.read_text(encoding="utf-8"))
    missing = sorted(set(data["expectedViews"]) - set(data["sidebarViews"]))
    extra = sorted(set(data["sidebarViews"]) - set(data["expectedViews"]))
    results.equal(PHASE, f"sidebar views match the {len(data['expectedViews'])} views in view_expectations.json",
                  ([], []), (missing, extra))
    for view in data["expectedViews"]:
        v = data["views"].get(view, {})
        problems = ([f"console: {c}" for c in v.get("console", [])]
                    + [f"api: {a}" for a in v.get("api", [])]
                    + [f"text: {t}" for t in v.get("text", [])]
                    + v.get("expect", []))
        for k in v.get("kpis", []):
            if k["got"] != k["want"]:
                problems.append(f"KPI '{k['label']}' shows {k['got']!r}, sent traffic means {k['want']!r}")
        results.equal(PHASE, f"view #{view}", [], problems[:4] + ([f"... {len(problems) - 4} more"] if len(problems) > 4 else []))
