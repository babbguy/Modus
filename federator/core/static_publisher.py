"""
Modus Federator — Static JSON Publisher
=============================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Pre-computes ALL read responses as static JSON files on disk.
Caddy serves these directly — zero Python overhead for reads.

This is the "serverless on a VPS" pattern:
    - Reads: Caddy serves static files (~10MB RAM)
    - Writes: Python starts on demand via socket activation
    - Aggregation: This script runs via cron, writes JSON, exits

Run via: python -m federator.core.static_publisher
Or via:  systemd timer (every hour)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import statistics as stats
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

def _get_static_dir() -> Path:
    """Static output directory — Caddy serves this. Read at call time, not import time."""
    return Path(os.environ.get("FEDERATOR_STATIC_DIR", "/data/static"))


def _ensure_dirs(static_dir: Path):
    """Create output directory structure."""
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "results").mkdir(exist_ok=True)
    (static_dir / "audit").mkdir(exist_ok=True)


def _write_json(path: Path, data: dict):
    """Atomically write JSON — write to temp, replace (no partial reads)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str))
    tmp.replace(path)  # replace() works cross-platform (rename() fails on Windows if target exists)


def publish_all(db_path: str):
    """
    Read from SQLite, compute aggregates, write static JSON files.

    This function is the ENTIRE read-side compute. After it runs,
    Caddy serves the JSON files directly. No Python needed for reads.
    """
    import sqlite3

    sd = _get_static_dir()
    _ensure_dirs(sd)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA mmap_size=67108864")  # 64MB mmap

    now = datetime.now(timezone.utc)
    current_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    min_contributors = int(os.environ.get("FEDERATOR_MIN_CONTRIBUTORS_FOR_RESULT", "3"))

    try:
        _publish_portal(conn, current_week, sd)
        _publish_benchmarks(conn, min_contributors, current_week, sd)
        _publish_merkle_root(conn, sd)
        logger.info("Static publish complete → %s", sd)
    finally:
        conn.close()


def _publish_portal(conn, current_week: str, sd: Path):
    """Write /v1/portal response."""
    rows = conn.execute("""
        SELECT industry_type,
               COUNT(*) as total,
               COUNT(DISTINCT nonce) as contributors,
               MAX(epoch_week) as latest_week
        FROM encrypted_deltas
        GROUP BY industry_type
        ORDER BY industry_type
    """).fetchall()

    industry_stats = []
    total_deltas = 0
    total_contributors = 0
    latest_week = None

    for r in rows:
        industry_stats.append({
            "industry": r["industry_type"],
            "total_deltas": r["total"],
            "unique_contributors": r["contributors"],
            "latest_epoch_week": r["latest_week"],
        })
        total_deltas += r["total"]
        total_contributors += r["contributors"]
        if r["latest_week"] and (latest_week is None or r["latest_week"] > latest_week):
            latest_week = r["latest_week"]

    merged_count = conn.execute("SELECT COUNT(*) FROM merged_results").fetchone()[0]

    anchor = conn.execute(
        "SELECT root_hash FROM merkle_anchors ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    from federator.config import settings

    _write_json(sd /"portal.json", {
        "service": "modus-federator",
        "version": settings.version,
        "available_industries": settings.allowed_industries,
        "industry_stats": industry_stats,
        "total_deltas": total_deltas,
        "total_contributors": total_contributors,
        "total_merged_results": merged_count,
        "latest_merkle_root": anchor["root_hash"] if anchor else None,
        "latest_epoch_week": latest_week,
    })


def _publish_benchmarks(conn, min_contributors: int, current_week: str, sd: Path):
    """Write /v1/benchmarks response."""
    # Get all industries with enough deltas
    groups = conn.execute("""
        SELECT industry_type, epoch_week,
               COUNT(DISTINCT nonce) as n_contributors
        FROM encrypted_deltas
        GROUP BY industry_type, epoch_week
        HAVING n_contributors >= ?
    """, (min_contributors,)).fetchall()

    benchmarks = []
    all_fitness = []
    all_genes = []
    all_spans = []
    all_nonces = []

    for g in groups:
        industry = g["industry_type"]
        epoch = g["epoch_week"]

        deltas = conn.execute("""
            SELECT fitness_improvement, gene_count, generation_span, nonce
            FROM encrypted_deltas
            WHERE industry_type = ? AND epoch_week = ?
        """, (industry, epoch)).fetchall()

        fitness = [d["fitness_improvement"] for d in deltas]
        genes = [d["gene_count"] for d in deltas]
        spans = [d["generation_span"] for d in deltas]
        nonces = [d["nonce"] for d in deltas]

        agg = _aggregate(fitness, genes, spans, nonces)

        benchmarks.append({
            "industry": industry,
            "avg_fitness_improvement": agg["fitness_trend"]["avg_improvement"],
            "active_contributors": agg["participating_instances"],
            "confidence": agg["confidence_score"],
            "epoch_week": epoch,
        })

        # Write per-industry result
        _write_json(sd /"results" / f"{industry}.json", {
            "version": len(benchmarks),
            "industry_type": industry,
            **agg,
            "epoch_week": epoch,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })

        all_fitness.extend(fitness)
        all_genes.extend(genes)
        all_spans.extend(spans)
        all_nonces.extend(nonces)

    # Cross-industry "all" result
    if all_fitness and len(set(all_nonces)) >= min_contributors:
        agg_all = _aggregate(all_fitness, all_genes, all_spans, all_nonces)
        _write_json(sd /"results" / "all.json", {
            "version": len(benchmarks) + 1,
            "industry_type": "all",
            **agg_all,
            "epoch_week": current_week,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })

    total_contributors = sum(b["active_contributors"] for b in benchmarks)
    _write_json(sd /"benchmarks.json", {
        "benchmarks": benchmarks,
        "total_industries": len(benchmarks),
        "total_contributors": total_contributors,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    })

    # Also write to DB for the write handler to reference
    _store_merged_results(conn, benchmarks, all_fitness, all_genes, all_spans, all_nonces, current_week)


def _store_merged_results(conn, benchmarks, all_fitness, all_genes, all_spans, all_nonces, current_week):
    """Persist merged results to DB (for audit and the write handler's reference)."""
    import uuid
    max_ver = conn.execute("SELECT COALESCE(MAX(version), 0) FROM merged_results").fetchone()[0]
    ver = max_ver + 1

    for b in benchmarks:
        conn.execute(
            "INSERT INTO merged_results (id, version, industry_type, aggregate_data, "
            "participating_instances, confidence_score, epoch_week) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), ver, b["industry"], json.dumps(b),
             b["active_contributors"], b["confidence"], b["epoch_week"]),
        )
        ver += 1

    if all_fitness and len(set(all_nonces)) >= 3:
        agg_all = _aggregate(all_fitness, all_genes, all_spans, all_nonces)
        conn.execute(
            "INSERT INTO merged_results (id, version, industry_type, aggregate_data, "
            "participating_instances, confidence_score, epoch_week) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), ver, "all", json.dumps(agg_all),
             agg_all["participating_instances"], agg_all["confidence_score"], current_week),
        )

    conn.commit()


def _publish_merkle_root(conn, sd: Path):
    """Build Merkle tree for current week and write audit JSON."""
    import hashlib
    import uuid

    now = datetime.now(timezone.utc)
    epoch_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

    leaves = [r[0] for r in conn.execute(
        "SELECT merkle_leaf_hash FROM encrypted_deltas WHERE epoch_week = ? ORDER BY received_at",
        (epoch_week,),
    ).fetchall()]

    if not leaves:
        _write_json(sd /"audit" / "merkle-root.json", {
            "root_hash": None, "leaf_count": 0, "epoch_week": epoch_week,
        })
        return

    # Build tree
    layer = leaves[:]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        next_layer = []
        for i in range(0, len(layer), 2):
            combined = bytes.fromhex(layer[i]) + bytes.fromhex(layer[i + 1])
            next_layer.append(hashlib.sha256(combined).hexdigest())
        layer = next_layer

    root_hash = layer[0]

    # Check if anchor exists
    existing = conn.execute(
        "SELECT id FROM merkle_anchors WHERE epoch_week = ? AND root_hash = ?",
        (epoch_week, root_hash),
    ).fetchone()

    if not existing:
        conn.execute(
            "INSERT INTO merkle_anchors (id, root_hash, leaf_count, epoch_week) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), root_hash, len(leaves), epoch_week),
        )
        conn.commit()

    _write_json(sd /"audit" / "merkle-root.json", {
        "root_hash": root_hash,
        "leaf_count": len(leaves),
        "epoch_week": epoch_week,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = pct * (len(sorted_vals) - 1)
    lower = int(idx)
    upper = min(lower + 1, len(sorted_vals) - 1)
    frac = idx - lower
    return round(sorted_vals[lower] + frac * (sorted_vals[upper] - sorted_vals[lower]), 6)


def _aggregate(fitness: list[float], genes: list[int], spans: list[int], nonces: list[str]) -> dict:
    """Pure-compute aggregation — same logic as blind_aggregator but without SQLAlchemy."""
    if not fitness:
        return {"fitness_trend": {}, "evolution_activity": {}, "participating_instances": 0, "confidence_score": 0.0}

    n = len(fitness)
    unique = len(set(nonces))
    sorted_f = sorted(fitness)
    avg_f = stats.mean(fitness)

    fitness_trend = {
        "avg_improvement": round(avg_f, 6),
        "median_improvement": round(stats.median(fitness), 6),
        "p25_improvement": _percentile(sorted_f, 0.25),
        "p75_improvement": _percentile(sorted_f, 0.75),
        "p90_improvement": _percentile(sorted_f, 0.90),
        "stddev": round(stats.stdev(fitness), 6) if n > 1 else 0.0,
        "trend_direction": "improving" if avg_f > 0 else "declining" if avg_f < 0 else "stable",
        "sample_size": n,
    }

    evolution_activity = {
        "avg_gene_count": round(stats.mean(genes), 2),
        "avg_generation_span": round(stats.mean(spans), 2),
        "median_gene_count": round(stats.median(genes), 2),
        "median_generation_span": round(stats.median(spans), 2),
        "active_contributors": unique,
    }

    contributor_factor = min(1.0, unique / 10.0)
    consistency_factor = 0.5
    if n > 1 and abs(avg_f) > 1e-9:
        cv = stats.stdev(fitness) / abs(avg_f)
        consistency_factor = max(0.0, 1.0 - min(1.0, cv))

    return {
        "fitness_trend": fitness_trend,
        "evolution_activity": evolution_activity,
        "participating_instances": unique,
        "confidence_score": round(0.6 * contributor_factor + 0.4 * consistency_factor, 4),
    }


# ── CLI entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_path = os.environ.get("FEDERATOR_DB_PATH", "/data/federator.db")
    if not os.path.exists(db_path):
        logger.error("Database not found: %s", db_path)
        sys.exit(1)
    publish_all(db_path)
