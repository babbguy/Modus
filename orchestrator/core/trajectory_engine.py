"""
Modus — Trajectory Engine (Phase 2)
========================================
Monte-Carlo session cost simulation, session fingerprinting, and agentic
tool-call vigilance checking.

Architecture
------------
Session Fingerprinting:
    Background task queries usage_records grouped by session_id, computing
    distribution statistics (mean, std, p95) for call count and cost per call.
    Fingerprints are upserted into the session_fingerprints table.

Trajectory Simulation:
    Two tiers — linear extrapolation (instant, zero-variance) and Monte-Carlo
    (200 runs, stdlib random, <10ms). Both produce TrajectoryResult with
    percentile-based cost projections and breach probability.

Vigil Check:
    Pattern matcher for agentic tool-call sequences. Detects runaway loops,
    deep recursion, blocked tool patterns, and amplification. Pure regex /
    string matching, <0.5ms, zero deps.

Complies with the Four Laws:
    - Pure Python, stdlib only (random, statistics, hashlib, re)
    - Zero external dependencies — no scipy, no numpy, no ML
    - All Monte-Carlo simulation <10ms for 200 runs
    - Thread-safe — no mutable module-level state
    - Non-blocking background fingerprint updates
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from orchestrator.db.session import _session_factory, sqlite_dt

logger = logging.getLogger(__name__)


# ── Data Structures ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionFingerprint:
    """
    Statistical fingerprint of a session type (NOT a SQLAlchemy model — the
    DB model lives in orchestrator.db.models). This is the in-memory
    computation struct used by the trajectory engine.
    """
    fingerprint_hash: str
    avg_calls_per_session: float
    std_calls: float
    avg_cost_per_call: float
    std_cost: float
    call_count_p95: float
    branching_factor: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class TrajectoryResult:
    """
    Result of a trajectory simulation — projected total session cost at
    multiple confidence levels, plus breach analysis.
    """
    p50_total_cost: float
    p75_total_cost: float
    p95_total_cost: float
    expected_remaining_calls: float
    breach_probability: float
    high_risk_paths: int


# ── Tier 1: Linear Extrapolation (instant, deterministic) ────────────────────


def linear_extrapolate(
    current_spend: float,
    current_calls: int,
    avg_cost_per_call: float,
    avg_calls_per_session: float,
) -> TrajectoryResult:
    """
    Simple linear extrapolation — no variance, no confidence intervals.

    remaining = avg_calls - current_calls  (floored at 0)
    projected = current_spend + remaining * avg_cost_per_call

    Returns TrajectoryResult with p50 = p75 = p95 = projected.
    Useful as an instant fallback when fingerprint data is sparse.
    """
    remaining = max(0.0, avg_calls_per_session - current_calls)
    projected = current_spend + remaining * avg_cost_per_call

    return TrajectoryResult(
        p50_total_cost=round(projected, 6),
        p75_total_cost=round(projected, 6),
        p95_total_cost=round(projected, 6),
        expected_remaining_calls=round(remaining, 2),
        breach_probability=0.0,
        high_risk_paths=0,
    )


# ── Tier 2: Monte-Carlo Simulation ───────────────────────────────────────────


def monte_carlo_simulate(
    current_spend: float,
    current_calls: int,
    fingerprint: SessionFingerprint,
    budget: float = 0.0,
    num_simulations: int = 200,
    confidence_level: float = 0.95,
) -> TrajectoryResult:
    """
    Pure-Python Monte-Carlo trajectory simulation.

    For each of `num_simulations` runs:
      1. Sample remaining calls from N(mean=avg_calls - current, std=std_calls)
         floored at 0.
      2. For each remaining call, sample cost from N(mean=avg_cost, std=std_cost)
         floored at 0.
      3. Total = current_spend + sum(sampled costs).

    Compute p50, p75, p95 from the distribution of totals.
    breach_probability = fraction of runs where total > budget (if budget > 0).

    Performance: 200 runs with ~20 remaining calls each ≈ 4000 random samples
    → well under 10ms on any hardware.
    """
    avg_remaining = max(0.0, fingerprint.avg_calls_per_session - current_calls)
    std_calls = max(0.0, fingerprint.std_calls)
    avg_cost = max(0.0, fingerprint.avg_cost_per_call)
    std_cost = max(0.0, fingerprint.std_cost)

    # Pre-seed a local Random for thread safety (no shared state)
    rng = random.Random()

    totals: list[float] = []
    breach_count = 0

    for _ in range(num_simulations):
        # Sample how many calls remain in this simulated session
        if std_calls > 1e-9:
            remaining = rng.gauss(avg_remaining, std_calls)
        else:
            remaining = avg_remaining
        remaining_int = max(0, int(round(remaining)))

        # Sample the cost of each remaining call
        session_cost = current_spend
        for _ in range(remaining_int):
            if std_cost > 1e-9:
                call_cost = max(0.0, rng.gauss(avg_cost, std_cost))
            else:
                call_cost = avg_cost
            session_cost += call_cost

        totals.append(session_cost)

        if budget > 0.0 and session_cost > budget:
            breach_count += 1

    # Sort for percentile extraction
    totals.sort()
    n = len(totals)

    def _percentile(pct: float) -> float:
        """Linear interpolation percentile from sorted list."""
        if n == 0:
            return 0.0
        k = (n - 1) * pct
        f = int(k)
        c = f + 1
        if c >= n:
            return totals[-1]
        return totals[f] + (k - f) * (totals[c] - totals[f])

    p50 = _percentile(0.50)
    p75 = _percentile(0.75)
    p95 = _percentile(confidence_level)

    # High-risk paths: simulations that exceed p95
    p95_threshold = _percentile(0.95)
    high_risk = sum(1 for t in totals if t > p95_threshold)

    breach_prob = breach_count / max(n, 1) if budget > 0.0 else 0.0

    return TrajectoryResult(
        p50_total_cost=round(p50, 6),
        p75_total_cost=round(p75, 6),
        p95_total_cost=round(p95, 6),
        expected_remaining_calls=round(avg_remaining, 2),
        breach_probability=round(breach_prob, 4),
        high_risk_paths=high_risk,
    )


# ── Vigil Check: Agentic Tool-Call Safety ─────────────────────────────────────


_DEFAULT_VIGIL_CONFIG: dict[str, Any] = {
    "max_tool_depth": 25,
    "max_loop_iterations": 5,
    "blocked_tool_patterns": [],
    "max_amplification_per_step": 10,
}


def vigil_check(
    tool_call_sequence: list[str],
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Pattern-match an ordered tool-call sequence for runaway behaviour.

    Detects:
      - Loops: same tool name appears consecutively N+ times
      - Recursion depth: total sequence length exceeds max_tool_depth
      - Blocked patterns: tool names matching forbidden regex patterns
      - Amplification: distinct tools per step exceeds threshold

    Config keys:
      max_tool_depth (int):             Max total calls in a session (default 25)
      max_loop_iterations (int):        Max consecutive identical calls (default 5)
      blocked_tool_patterns (list[str]):Regex patterns for forbidden tools (default [])
      max_amplification_per_step (int): Max branching factor per step (default 10)

    Returns:
      {"safe": bool, "reason": str, "risk_score": float}

    Performance: pure string/regex ops, <0.5ms for sequences up to 1000 calls.
    """
    cfg = {**_DEFAULT_VIGIL_CONFIG, **(config or {})}
    max_depth = cfg["max_tool_depth"]
    max_loop = cfg["max_loop_iterations"]
    blocked_patterns: list[str] = cfg["blocked_tool_patterns"]
    max_amp = cfg["max_amplification_per_step"]

    seq_len = len(tool_call_sequence)

    # ── Check 1: Depth exceeded ───────────────────────────────────────────
    if seq_len > max_depth:
        return {
            "safe": False,
            "reason": f"Tool depth {seq_len} exceeds limit {max_depth}",
            "risk_score": min(1.0, seq_len / max(max_depth, 1)),
        }

    # ── Check 2: Loop detection (consecutive identical calls) ─────────────
    if seq_len > 0 and max_loop > 0:
        run_len = 1
        for i in range(1, seq_len):
            if tool_call_sequence[i] == tool_call_sequence[i - 1]:
                run_len += 1
                if run_len > max_loop:
                    return {
                        "safe": False,
                        "reason": (
                            f"Loop detected: '{tool_call_sequence[i]}' called "
                            f"{run_len} consecutive times (limit {max_loop})"
                        ),
                        "risk_score": min(1.0, run_len / max(max_loop, 1)),
                    }
            else:
                run_len = 1

    # ── Check 3: Blocked tool patterns ────────────────────────────────────
    for tool_name in tool_call_sequence:
        for pattern in blocked_patterns:
            try:
                if re.search(pattern, tool_name):
                    return {
                        "safe": False,
                        "reason": f"Blocked tool pattern '{pattern}' matched '{tool_name}'",
                        "risk_score": 1.0,
                    }
            except re.error:
                # Invalid regex in config — skip rather than crash the hot path
                continue

    # ── Check 4: Amplification (distinct tools in a sliding window) ───────
    if seq_len > max_amp:
        window_size = min(max_amp * 2, seq_len)
        for start in range(0, seq_len - window_size + 1):
            window = tool_call_sequence[start : start + window_size]
            distinct = len(set(window))
            if distinct > max_amp:
                return {
                    "safe": False,
                    "reason": (
                        f"Amplification detected: {distinct} distinct tools in "
                        f"window of {window_size} (limit {max_amp})"
                    ),
                    "risk_score": min(1.0, distinct / max(max_amp, 1)),
                }

    # ── All clear ─────────────────────────────────────────────────────────
    # Compute a risk score from 0.0 (empty) to ~0.8 (close to limits)
    depth_ratio = seq_len / max(max_depth, 1) if max_depth > 0 else 0.0
    risk = round(min(0.8, depth_ratio * 0.8), 4)

    return {
        "safe": True,
        "reason": "ok",
        "risk_score": risk,
    }


# ── Session Fingerprinting (Background) ──────────────────────────────────────


def _compute_p95(values: list[float]) -> float:
    """Percentile-95 from a sorted list. Pure Python."""
    if not values:
        return 0.0
    sv = sorted(values)
    n = len(sv)
    k = (n - 1) * 0.95
    f = int(k)
    c = f + 1
    if c >= n:
        return sv[-1]
    return sv[f] + (k - f) * (sv[c] - sv[f])


def _fingerprint_hash(
    app_id: str,
    provider: str,
    model: str,
) -> str:
    """Deterministic hash for a (app, provider, model) fingerprint group."""
    key = f"{app_id}|{provider}|{model}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def compute_session_fingerprints(
    db: Any,
    lookback_days: int = 28,
) -> list[dict[str, Any]]:
    """
    Query usage_records grouped by session_id, compute distribution
    statistics for call count and cost per call.

    Returns a list of dicts suitable for upserting into the
    session_fingerprints table (or equivalent).

    Uses raw SQL for portability across SQLite and PostgreSQL.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=lookback_days)

    # Step 1: Per-session aggregates
    q = text("""
        SELECT
            app_id,
            provider,
            COALESCE(model, 'unknown')   AS model,
            session_id,
            COUNT(*)                     AS call_count,
            SUM(total_cost)              AS session_cost
        FROM usage_records
        WHERE session_id IS NOT NULL
          AND timestamp >= :window_start
        GROUP BY app_id, provider, model, session_id
    """)
    result = await db.execute(q, {"window_start": sqlite_dt(window_start)})
    rows = result.all()

    if not rows:
        return []

    # Step 2: Group by (app, provider, model) → list of per-session stats
    groups: dict[str, list[tuple[int, float]]] = {}
    for r in rows:
        key = f"{r.app_id}|{r.provider}|{r.model}"
        if key not in groups:
            groups[key] = []
        groups[key].append((int(r.call_count), float(r.session_cost or 0)))

    # Step 3: Compute fingerprint statistics per group
    fingerprints: list[dict[str, Any]] = []
    for key, sessions in groups.items():
        parts = key.split("|", 2)
        app_id, provider, model = parts[0], parts[1], parts[2]
        sample_count = len(sessions)

        if sample_count < 2:
            continue  # Need at least 2 sessions for meaningful stats

        call_counts = [float(s[0]) for s in sessions]
        costs_per_call = [
            s[1] / max(s[0], 1) for s in sessions
        ]

        avg_calls = statistics.mean(call_counts)
        std_calls = statistics.stdev(call_counts) if sample_count > 1 else 0.0
        avg_cost = statistics.mean(costs_per_call)
        std_cost = statistics.stdev(costs_per_call) if sample_count > 1 else 0.0
        p95_calls = _compute_p95(call_counts)

        # Branching factor: ratio of distinct models used within sessions
        # to total sessions. Higher = more diverse model usage.
        branching = len(set(s[0] for s in sessions)) / max(sample_count, 1)

        fp_hash = _fingerprint_hash(app_id, provider, model)

        fingerprints.append({
            "fingerprint_hash": fp_hash,
            "app_id": app_id,
            "provider": provider,
            "model": model,
            "avg_calls_per_session": round(avg_calls, 4),
            "std_calls": round(std_calls, 4),
            "avg_cost_per_call": round(avg_cost, 8),
            "std_cost": round(std_cost, 8),
            "call_count_p95": round(p95_calls, 2),
            "branching_factor": round(branching, 4),
            "sample_count": sample_count,
            "computed_at": now.isoformat(),
        })

    return fingerprints


# ── Background Task Entry Point ──────────────────────────────────────────────


async def run_trajectory_fingerprint_update() -> None:
    """
    Background task: compute session fingerprints and upsert into DB.

    Follows the insights_engine pattern — called from the task scheduler,
    runs inside the existing async event loop. No separate process required.
    """
    if _session_factory is None:
        return

    start = time.perf_counter()

    try:
        async with _session_factory() as db:
            fingerprints = await compute_session_fingerprints(db)

            if not fingerprints:
                logger.debug("Trajectory fingerprint update: no sessions found")
                return

            # Upsert fingerprints — use INSERT OR REPLACE for SQLite compat,
            # ON CONFLICT for PostgreSQL. Detect engine from session bind.
            upserted = 0
            for fp in fingerprints:
                # Try portable upsert: delete + insert (works on both engines)
                await db.execute(
                    text(
                        "DELETE FROM session_fingerprints "
                        "WHERE fingerprint_hash = :fingerprint_hash"
                    ),
                    {"fingerprint_hash": fp["fingerprint_hash"]},
                )
                await db.execute(
                    text("""
                        INSERT INTO session_fingerprints (
                            fingerprint_hash, app_id, provider, model,
                            avg_calls_per_session, std_calls,
                            avg_cost_per_call, std_cost,
                            call_count_p95, branching_factor,
                            sample_count, computed_at
                        ) VALUES (
                            :fingerprint_hash, :app_id, :provider, :model,
                            :avg_calls_per_session, :std_calls,
                            :avg_cost_per_call, :std_cost,
                            :call_count_p95, :branching_factor,
                            :sample_count, :computed_at
                        )
                    """),
                    fp,
                )
                upserted += 1

            await db.commit()

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                "Trajectory fingerprint update: %d fingerprints in %.1fms",
                upserted, elapsed,
            )

    except Exception as exc:
        logger.error("Trajectory fingerprint update failed: %s", exc, exc_info=True)
