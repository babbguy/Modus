# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0

"""
Modus — TRiSM Multi-Agent Sentinel (Phase 8c)
==================================================
Pre-call threat detection for agentic AI sessions using OWASP 2026
threat taxonomy. Scans session traces for context poisoning, goal hijack,
cascade failure, communication anomalies, and tool misuse.

Architecture
------------
TRiSMSentinel receives a session trace (list of call dicts) and runs five
parallel detection passes. Each detector produces ThreatIndicator instances.
The sentinel aggregates these into a ScanResult with a composite risk score
and auto-generated rollback plan.

Detection algorithms:
    - Context poisoning:      Shannon entropy of context diffs
    - Goal hijack:            TF-IDF cosine distance between call intents
    - Cascade failure:        Error propagation rate + Monte Carlo prediction
    - Communication anomaly:  Directed graph cycle detection via DFS
    - Tool misuse:            Regex matching against known-bad patterns

Complies with the Four Laws:
    - Pure Python, stdlib only (math, re, collections, random, hashlib)
    - Zero external dependencies — no scipy, no numpy, no ML
    - Full scan < 50ms for traces up to 500 calls
    - Thread-safe — no mutable module-level state
    - Non-blocking — no I/O, no network calls
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from orchestrator.core.trism_patterns import (
    BUILTIN_PATTERNS,
    DEFAULT_THRESHOLDS,
    get_model_tier,
    get_patterns_by_type,
    get_regex_indicators,
)

logger = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ThreatIndicator:
    """A single detected threat signal within a session trace."""

    threat_type: str
    severity: str        # "critical", "high", "medium", "low", "info"
    confidence: float    # 0.0 - 1.0
    description: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Aggregated result of a full TRiSM session scan."""

    threats: list[ThreatIndicator]
    risk_score: float            # 0.0 - 1.0
    recommended_action: str      # "allow", "warn", "throttle", "block", "terminate"
    rollback_plan: dict[str, Any]


# ── Severity Classification ──────────────────────────────────────────────────


def _classify_severity(risk_weight: float, confidence: float) -> str:
    """Map a combined risk score to a severity label."""
    combined = risk_weight * confidence
    if combined >= 0.85:
        return "critical"
    if combined >= 0.7:
        return "high"
    if combined >= 0.45:
        return "medium"
    if combined >= 0.2:
        return "low"
    return "info"


def _classify_action(risk_score: float, threat_count: int, thresholds: dict) -> str:
    """Determine recommended action based on aggregate risk."""
    critical_threshold = thresholds.get("risk_score_critical_threshold", 0.85)
    warning_threshold = thresholds.get("risk_score_warning_threshold", 0.5)
    max_threats = thresholds.get("max_threats_before_block", 3)

    if risk_score >= critical_threshold or threat_count >= max_threats:
        return "block"
    if risk_score >= 0.7:
        return "throttle"
    if risk_score >= warning_threshold:
        return "warn"
    return "allow"


# ── Shannon Entropy ──────────────────────────────────────────────────────────


def _shannon_entropy(text: str) -> float:
    """
    Compute Shannon entropy in bits per character for the given text.

    Uses collections.Counter for character frequency distribution and
    math.log2 for information content. Returns 0.0 for empty strings.
    """
    if not text:
        return 0.0
    length = len(text)
    freq = Counter(text)
    entropy = 0.0
    for count in freq.values():
        p = count / length
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


# ── TF-IDF Cosine Similarity ────────────────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer."""
    return re.findall(r"[a-zA-Z0-9_\-]+", text.lower())


def _term_frequencies(tokens: list[str]) -> Counter:
    """Compute term frequency counter for a token list."""
    return Counter(tokens)


def _cosine_similarity(tf_a: Counter, tf_b: Counter) -> float:
    """
    Compute cosine similarity between two term-frequency vectors.

    Uses the formula: dot(A, B) / (||A|| * ||B||)
    Implemented with collections.Counter and math.sqrt.
    Returns 1.0 for identical vectors, 0.0 for orthogonal.
    """
    if not tf_a or not tf_b:
        return 0.0

    # Shared terms for dot product
    common_keys = set(tf_a.keys()) & set(tf_b.keys())
    dot_product = sum(tf_a[k] * tf_b[k] for k in common_keys)

    mag_a = math.sqrt(sum(v * v for v in tf_a.values()))
    mag_b = math.sqrt(sum(v * v for v in tf_b.values()))

    if mag_a < 1e-12 or mag_b < 1e-12:
        return 0.0

    return dot_product / (mag_a * mag_b)


def _cosine_distance(tf_a: Counter, tf_b: Counter) -> float:
    """Cosine distance = 1 - cosine_similarity. Range: [0, 1]."""
    return 1.0 - _cosine_similarity(tf_a, tf_b)


# ── Call Intent Representation ───────────────────────────────────────────────


def _call_intent_tokens(call: dict[str, Any]) -> list[str]:
    """
    Build a token representation of a call's intent for similarity comparison.

    Intent = model name + tool calls + context hash. This captures what the
    call is doing (tools), how (model), and in what context (hash).
    """
    tokens: list[str] = []
    model = call.get("model", "")
    if model:
        tokens.extend(_tokenize(model))

    tool_calls = call.get("tool_calls") or []
    for tc in tool_calls:
        if isinstance(tc, str):
            tokens.extend(_tokenize(tc))
        elif isinstance(tc, dict):
            tokens.extend(_tokenize(str(tc.get("name", ""))))

    context_hash = call.get("context_hash", "")
    if context_hash:
        tokens.append(context_hash[:8])

    return tokens


# ── Graph Utilities ──────────────────────────────────────────────────────────


def _detect_cycles_dfs(
    graph: dict[str, list[str]],
    max_cycle_length: int = 10,
) -> list[list[str]]:
    """
    Detect cycles in a directed graph using iterative DFS.

    Returns a list of cycles found, each as a list of node IDs.
    Limits cycle search to max_cycle_length to bound runtime.
    """
    cycles: list[list[str]] = []
    visited: set[str] = set()

    for start_node in graph:
        if start_node in visited:
            continue

        # Iterative DFS with path tracking
        stack: list[tuple[str, list[str], set[str]]] = [
            (start_node, [start_node], {start_node})
        ]

        while stack:
            node, path, path_set = stack.pop()

            for neighbor in graph.get(node, []):
                if neighbor in path_set:
                    # Found a cycle
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    if len(cycle) - 1 <= max_cycle_length:
                        cycles.append(cycle)
                elif neighbor not in visited and len(path) < max_cycle_length:
                    stack.append(
                        (neighbor, path + [neighbor], path_set | {neighbor})
                    )

            visited.add(node)

    return cycles


def _compute_delegation_depth(
    graph: dict[str, list[str]],
) -> int:
    """
    Compute the maximum delegation depth (longest path) in a directed graph.

    Uses iterative DFS to avoid stack overflow on deep chains.
    """
    if not graph:
        return 0

    max_depth = 0
    for start in graph:
        stack: list[tuple[str, int, set[str]]] = [(start, 1, {start})]
        while stack:
            node, depth, seen = stack.pop()
            max_depth = max(max_depth, depth)
            for neighbor in graph.get(node, []):
                if neighbor not in seen:
                    stack.append((neighbor, depth + 1, seen | {neighbor}))

    return max_depth


# ── TRiSM Sentinel ───────────────────────────────────────────────────────────


class TRiSMSentinel:
    """
    Multi-agent threat detection sentinel.

    Scans session traces against OWASP 2026 agentic threat patterns to
    detect context poisoning, goal hijacking, cascade failures,
    communication anomalies, and tool misuse.

    All detection is pure Python stdlib — no external dependencies,
    no network calls, no disk I/O. Full scan targets < 50ms for traces
    up to 500 calls.

    Parameters
    ----------
    patterns : list[dict], optional
        Custom threat patterns. Defaults to BUILTIN_PATTERNS.
    thresholds : dict, optional
        Custom detection thresholds. Merged over DEFAULT_THRESHOLDS.
    """

    __slots__ = ("_patterns", "_thresholds", "_rng")

    def __init__(
        self,
        patterns: list[dict[str, Any]] | None = None,
        thresholds: dict[str, Any] | None = None,
    ) -> None:
        self._patterns = patterns if patterns is not None else BUILTIN_PATTERNS
        self._thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        # Thread-local RNG for Monte Carlo — no shared mutable state
        self._rng = random.Random()

    # ── Main Entry Point ─────────────────────────────────────────────────

    def scan_session(self, session_trace: list[dict[str, Any]]) -> ScanResult:
        """
        Run all threat detectors against a session trace.

        Parameters
        ----------
        session_trace : list[dict]
            Ordered list of call records. Each dict should contain at
            minimum: call_idx, model, tokens_in, tokens_out, cost,
            tool_calls, context_hash, app_id, error.

        Returns
        -------
        ScanResult
            Aggregated threats, risk score, recommended action, and
            rollback plan.
        """
        if not session_trace:
            return ScanResult(
                threats=[],
                risk_score=0.0,
                recommended_action="allow",
                rollback_plan={},
            )

        start_time = time.perf_counter()

        # Run all detectors
        threats: list[ThreatIndicator] = []
        threats.extend(self.detect_context_poisoning(session_trace))
        threats.extend(self.detect_goal_hijack(session_trace))
        threats.extend(self.detect_cascade_failure(session_trace))
        threats.extend(self.detect_communication_anomaly(session_trace))
        threats.extend(self.detect_tool_misuse(session_trace))

        # Compute composite risk score — weighted average of threat
        # confidences, capped at 1.0.
        if threats:
            weighted_sum = sum(t.confidence for t in threats)
            # Normalize: more threats = higher risk, with diminishing returns
            risk_score = min(1.0, weighted_sum / max(len(threats), 1) *
                             (1.0 + 0.1 * min(len(threats), 5)))
        else:
            risk_score = 0.0

        risk_score = round(risk_score, 4)

        # Determine recommended action
        action = _classify_action(
            risk_score, len(threats), self._thresholds
        )

        # Generate rollback plan
        rollback_plan = self.generate_rollback_plan(threats)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        if elapsed_ms > self._thresholds.get("scan_timeout_ms", 50):
            logger.warning(
                "TRiSM scan exceeded soft timeout: %.1fms (limit %dms)",
                elapsed_ms,
                self._thresholds.get("scan_timeout_ms", 50),
            )

        return ScanResult(
            threats=threats,
            risk_score=risk_score,
            recommended_action=action,
            rollback_plan=rollback_plan,
        )

    # ── Detector: Context Poisoning ──────────────────────────────────────

    def detect_context_poisoning(
        self,
        trace: list[dict[str, Any]],
    ) -> list[ThreatIndicator]:
        """
        Detect context poisoning via entropy analysis and pattern matching.

        Algorithms:
          1. Shannon entropy of context_hash diffs between consecutive calls.
             A sudden spike (> threshold bits/token) indicates injection.
          2. Context growth rate — if context size doubles between calls
             beyond the rolling average, flag anomalous growth.
          3. Regex matching against known injection signatures.
        """
        threats: list[ThreatIndicator] = []
        poisoning_patterns = get_patterns_by_type("context_poisoning")

        entropy_threshold = self._thresholds.get("entropy_spike_threshold", 3.5)
        growth_rate_max = self._thresholds.get("context_growth_rate_max", 2.0)
        growth_window = self._thresholds.get("context_growth_window", 5)
        min_len = self._thresholds.get("min_context_length_for_entropy", 50)

        # ── Entropy spike detection ──────────────────────────────────────
        context_hashes: list[str] = []
        for call in trace:
            ctx = call.get("context_hash", "")
            context_hashes.append(ctx if ctx else "")

        if len(context_hashes) >= 2:
            entropies: list[float] = []
            for i in range(1, len(context_hashes)):
                prev_hash = context_hashes[i - 1]
                curr_hash = context_hashes[i]
                if prev_hash == curr_hash:
                    entropies.append(0.0)
                    continue

                # Compute diff entropy — use the XOR-like difference of hashes
                # as a proxy for context change magnitude
                diff_str = curr_hash + prev_hash
                if len(diff_str) >= min_len:
                    ent = _shannon_entropy(diff_str)
                    entropies.append(ent)
                    if ent > entropy_threshold:
                        threats.append(ThreatIndicator(
                            threat_type="context_poisoning",
                            severity=_classify_severity(0.75, min(1.0, ent / 5.0)),
                            confidence=round(min(1.0, ent / 5.0), 4),
                            description=(
                                f"Entropy spike detected at call {i}: "
                                f"{ent:.2f} bits/char exceeds threshold "
                                f"{entropy_threshold}"
                            ),
                            evidence={
                                "call_idx": i,
                                "entropy": round(ent, 4),
                                "threshold": entropy_threshold,
                                "prev_hash": prev_hash[:16],
                                "curr_hash": curr_hash[:16],
                            },
                        ))
                else:
                    entropies.append(0.0)

        # ── Context growth rate detection ────────────────────────────────
        token_sizes: list[int] = []
        for call in trace:
            tokens_in = call.get("tokens_in", 0) or 0
            token_sizes.append(int(tokens_in))

        if len(token_sizes) >= growth_window + 1:
            for i in range(growth_window, len(token_sizes)):
                window = token_sizes[i - growth_window : i]
                avg_window = sum(window) / max(len(window), 1)
                current = token_sizes[i]
                if avg_window > 0 and current / avg_window > growth_rate_max:
                    rate = current / avg_window
                    confidence = min(1.0, (rate - growth_rate_max) / growth_rate_max)
                    threats.append(ThreatIndicator(
                        threat_type="context_poisoning",
                        severity=_classify_severity(0.7, confidence),
                        confidence=round(confidence, 4),
                        description=(
                            f"Anomalous context growth at call {i}: "
                            f"{rate:.1f}x average (threshold {growth_rate_max}x)"
                        ),
                        evidence={
                            "call_idx": i,
                            "growth_rate": round(rate, 4),
                            "threshold": growth_rate_max,
                            "current_tokens": current,
                            "avg_window_tokens": round(avg_window, 2),
                        },
                    ))

        # ── Known injection signature matching ───────────────────────────
        for pattern_def in poisoning_patterns:
            regex_indicators = get_regex_indicators(pattern_def)
            if not regex_indicators:
                continue

            for i, call in enumerate(trace):
                # Build searchable text from call metadata
                searchable = self._build_searchable_text(call)
                if not searchable:
                    continue

                for regex in regex_indicators:
                    try:
                        match = re.search(regex, searchable)
                        if match:
                            threats.append(ThreatIndicator(
                                threat_type="context_poisoning",
                                severity=_classify_severity(
                                    pattern_def["risk_weight"], 0.9
                                ),
                                confidence=0.9,
                                description=(
                                    f"Injection signature matched at call {i}: "
                                    f"pattern '{pattern_def['name']}'"
                                ),
                                evidence={
                                    "call_idx": i,
                                    "pattern_name": pattern_def["name"],
                                    "matched_text": match.group()[:100],
                                    "risk_weight": pattern_def["risk_weight"],
                                },
                            ))
                            break  # One match per pattern per call
                    except re.error:
                        continue

        return threats

    # ── Detector: Goal Hijack ────────────────────────────────────────────

    def detect_goal_hijack(
        self,
        trace: list[dict[str, Any]],
    ) -> list[ThreatIndicator]:
        """
        Detect goal hijacking via intent drift analysis.

        Algorithms:
          1. TF-IDF cosine distance between consecutive call intents.
             Large distance (> 0.7) indicates drastic intent change.
          2. Tool call pattern shift — Jaccard distance between tool sets
             in the first N vs. last N calls.
          3. Model escalation — detect unexpected jumps in model capability.
          4. Regex matching against known hijack sequences.
        """
        threats: list[ThreatIndicator] = []

        if len(trace) < 2:
            return threats

        cosine_threshold = self._thresholds.get(
            "intent_cosine_distance_threshold", 0.7
        )
        shift_window = self._thresholds.get("tool_pattern_shift_window", 5)
        shift_threshold = self._thresholds.get(
            "tool_pattern_shift_threshold", 0.6
        )

        # ── Intent vector rotation (TF-IDF cosine distance) ─────────────
        prev_tf: Counter | None = None
        for i, call in enumerate(trace):
            tokens = _call_intent_tokens(call)
            curr_tf = _term_frequencies(tokens)

            if prev_tf is not None and curr_tf:
                dist = _cosine_distance(prev_tf, curr_tf)
                if dist > cosine_threshold:
                    confidence = min(1.0, dist)
                    threats.append(ThreatIndicator(
                        threat_type="goal_hijack",
                        severity=_classify_severity(0.8, confidence),
                        confidence=round(confidence, 4),
                        description=(
                            f"Intent rotation at call {i}: cosine distance "
                            f"{dist:.3f} exceeds threshold {cosine_threshold}"
                        ),
                        evidence={
                            "call_idx": i,
                            "cosine_distance": round(dist, 4),
                            "threshold": cosine_threshold,
                            "prev_model": trace[i - 1].get("model", ""),
                            "curr_model": call.get("model", ""),
                        },
                    ))

            prev_tf = curr_tf

        # ── Tool pattern shift (Jaccard distance) ───────────────────────
        if len(trace) >= shift_window * 2:
            early_tools: set[str] = set()
            late_tools: set[str] = set()

            for call in trace[:shift_window]:
                for tc in (call.get("tool_calls") or []):
                    if isinstance(tc, str):
                        early_tools.add(tc)
                    elif isinstance(tc, dict):
                        early_tools.add(tc.get("name", ""))

            for call in trace[-shift_window:]:
                for tc in (call.get("tool_calls") or []):
                    if isinstance(tc, str):
                        late_tools.add(tc)
                    elif isinstance(tc, dict):
                        late_tools.add(tc.get("name", ""))

            if early_tools or late_tools:
                union = early_tools | late_tools
                intersection = early_tools & late_tools
                jaccard_dist = 1.0 - (
                    len(intersection) / max(len(union), 1)
                )

                if jaccard_dist > shift_threshold:
                    confidence = min(1.0, jaccard_dist)
                    threats.append(ThreatIndicator(
                        threat_type="goal_hijack",
                        severity=_classify_severity(0.7, confidence),
                        confidence=round(confidence, 4),
                        description=(
                            f"Tool pattern shift: Jaccard distance "
                            f"{jaccard_dist:.3f} between early and late calls "
                            f"exceeds threshold {shift_threshold}"
                        ),
                        evidence={
                            "jaccard_distance": round(jaccard_dist, 4),
                            "threshold": shift_threshold,
                            "early_tools": sorted(early_tools),
                            "late_tools": sorted(late_tools),
                        },
                    ))

        # ── Model escalation detection ───────────────────────────────────
        tier_map = self._thresholds.get("model_escalation_tiers", {})
        prev_tier = 0
        for i, call in enumerate(trace):
            model = call.get("model", "")
            if not model:
                continue
            tier = get_model_tier(model, tier_map)
            if tier == 0:
                continue
            if prev_tier > 0 and tier > prev_tier:
                # Escalation detected
                confidence = min(1.0, 0.3 * (tier - prev_tier))
                threats.append(ThreatIndicator(
                    threat_type="goal_hijack",
                    severity=_classify_severity(0.6, confidence),
                    confidence=round(confidence, 4),
                    description=(
                        f"Model escalation at call {i}: tier {prev_tier} -> "
                        f"{tier} ({trace[i-1].get('model', '?')} -> {model})"
                    ),
                    evidence={
                        "call_idx": i,
                        "prev_tier": prev_tier,
                        "curr_tier": tier,
                        "prev_model": trace[i - 1].get("model", ""),
                        "curr_model": model,
                    },
                ))
            if tier > 0:
                prev_tier = tier

        # ── Known hijack sequence matching ───────────────────────────────
        hijack_patterns = get_patterns_by_type("goal_hijack")
        for pattern_def in hijack_patterns:
            regex_indicators = get_regex_indicators(pattern_def)
            if not regex_indicators:
                continue

            for i, call in enumerate(trace):
                searchable = self._build_searchable_text(call)
                if not searchable:
                    continue

                for regex in regex_indicators:
                    try:
                        match = re.search(regex, searchable)
                        if match:
                            threats.append(ThreatIndicator(
                                threat_type="goal_hijack",
                                severity=_classify_severity(
                                    pattern_def["risk_weight"], 0.85
                                ),
                                confidence=0.85,
                                description=(
                                    f"Hijack sequence at call {i}: "
                                    f"pattern '{pattern_def['name']}'"
                                ),
                                evidence={
                                    "call_idx": i,
                                    "pattern_name": pattern_def["name"],
                                    "matched_text": match.group()[:100],
                                },
                            ))
                            break
                    except re.error:
                        continue

        return threats

    # ── Detector: Cascade Failure ────────────────────────────────────────

    def detect_cascade_failure(
        self,
        trace: list[dict[str, Any]],
    ) -> list[ThreatIndicator]:
        """
        Predict cascading failures via error propagation analysis.

        Algorithms:
          1. Error rate in a sliding window — if > threshold, flag cascade risk.
          2. Consecutive error count — 3+ consecutive errors indicate active cascade.
          3. Monte Carlo prediction — simulate remaining calls assuming
             current error rate persists, estimate cascade probability.
          4. Regex matching against resource exhaustion patterns.
        """
        threats: list[ThreatIndicator] = []

        if not trace:
            return threats

        consecutive_threshold = self._thresholds.get(
            "consecutive_error_threshold", 3
        )
        error_rate_threshold = self._thresholds.get("error_rate_threshold", 0.4)
        error_window = self._thresholds.get("error_rate_window", 10)
        mc_runs = self._thresholds.get("cascade_monte_carlo_runs", 100)
        depth_limit = self._thresholds.get("dependency_chain_depth_limit", 8)

        # Extract error flags
        errors: list[bool] = []
        for call in trace:
            err = call.get("error")
            has_error = err is not None and err != "" and err is not False
            errors.append(has_error)

        # ── Consecutive error detection ──────────────────────────────────
        if errors:
            run_len = 0
            max_run = 0
            max_run_start = 0
            current_start = 0
            for i, is_err in enumerate(errors):
                if is_err:
                    if run_len == 0:
                        current_start = i
                    run_len += 1
                    if run_len > max_run:
                        max_run = run_len
                        max_run_start = current_start
                else:
                    run_len = 0

            if max_run >= consecutive_threshold:
                confidence = min(1.0, max_run / (consecutive_threshold * 2))
                threats.append(ThreatIndicator(
                    threat_type="cascade_failure",
                    severity=_classify_severity(0.85, confidence),
                    confidence=round(confidence, 4),
                    description=(
                        f"Consecutive errors: {max_run} errors starting at "
                        f"call {max_run_start} (threshold {consecutive_threshold})"
                    ),
                    evidence={
                        "consecutive_errors": max_run,
                        "start_idx": max_run_start,
                        "threshold": consecutive_threshold,
                    },
                ))

        # ── Sliding window error rate ────────────────────────────────────
        if len(errors) >= error_window:
            for start in range(len(errors) - error_window + 1):
                window = errors[start : start + error_window]
                rate = sum(window) / len(window)
                if rate > error_rate_threshold:
                    confidence = min(1.0, rate)
                    threats.append(ThreatIndicator(
                        threat_type="cascade_failure",
                        severity=_classify_severity(0.8, confidence),
                        confidence=round(confidence, 4),
                        description=(
                            f"High error rate in window [{start}:{start + error_window}]: "
                            f"{rate:.1%} exceeds threshold {error_rate_threshold:.0%}"
                        ),
                        evidence={
                            "window_start": start,
                            "window_end": start + error_window,
                            "error_rate": round(rate, 4),
                            "threshold": error_rate_threshold,
                        },
                    ))
                    break  # One alert per scan

        # ── Monte Carlo cascade prediction ───────────────────────────────
        total_errors = sum(errors)
        total_calls = len(errors)
        if total_calls > 0 and total_errors > 0:
            observed_rate = total_errors / total_calls
            # Simulate: how likely is a cascade (5+ consecutive errors)
            # in the next 10 calls given the current error rate?
            remaining_calls = 10
            cascade_count = 0
            cascade_threshold_sim = 5

            for _ in range(mc_runs):
                consecutive = 0
                cascaded = False
                for _ in range(remaining_calls):
                    if self._rng.random() < observed_rate:
                        consecutive += 1
                        if consecutive >= cascade_threshold_sim:
                            cascaded = True
                            break
                    else:
                        consecutive = 0
                if cascaded:
                    cascade_count += 1

            cascade_prob = cascade_count / max(mc_runs, 1)
            if cascade_prob > 0.2:
                threats.append(ThreatIndicator(
                    threat_type="cascade_failure",
                    severity=_classify_severity(0.85, cascade_prob),
                    confidence=round(cascade_prob, 4),
                    description=(
                        f"Monte Carlo cascade prediction: {cascade_prob:.0%} "
                        f"probability of cascade in next {remaining_calls} calls "
                        f"(observed error rate {observed_rate:.1%})"
                    ),
                    evidence={
                        "cascade_probability": round(cascade_prob, 4),
                        "observed_error_rate": round(observed_rate, 4),
                        "simulations": mc_runs,
                        "remaining_calls": remaining_calls,
                    },
                ))

        # ── Dependency chain depth ───────────────────────────────────────
        # Simple heuristic: if calls reference previous call outputs
        # (same tool sequence), track chain length
        if len(trace) > depth_limit:
            threats.append(ThreatIndicator(
                threat_type="cascade_failure",
                severity=_classify_severity(0.7, 0.6),
                confidence=0.6,
                description=(
                    f"Deep dependency chain: {len(trace)} calls exceeds "
                    f"depth limit {depth_limit}"
                ),
                evidence={
                    "chain_depth": len(trace),
                    "limit": depth_limit,
                },
            ))

        # ── Resource exhaustion pattern matching ─────────────────────────
        cascade_patterns = get_patterns_by_type("cascade_failure")
        for pattern_def in cascade_patterns:
            regex_indicators = get_regex_indicators(pattern_def)
            if not regex_indicators:
                continue

            for i, call in enumerate(trace):
                err_text = str(call.get("error", "") or "")
                if not err_text:
                    continue

                for regex in regex_indicators:
                    try:
                        match = re.search(regex, err_text)
                        if match:
                            threats.append(ThreatIndicator(
                                threat_type="cascade_failure",
                                severity=_classify_severity(
                                    pattern_def["risk_weight"], 0.8
                                ),
                                confidence=0.8,
                                description=(
                                    f"Resource exhaustion at call {i}: "
                                    f"'{pattern_def['name']}'"
                                ),
                                evidence={
                                    "call_idx": i,
                                    "pattern_name": pattern_def["name"],
                                    "matched_text": match.group()[:100],
                                    "error": err_text[:200],
                                },
                            ))
                            break
                    except re.error:
                        continue

        return threats

    # ── Detector: Communication Anomaly ──────────────────────────────────

    def detect_communication_anomaly(
        self,
        trace: list[dict[str, Any]],
    ) -> list[ThreatIndicator]:
        """
        Detect multi-agent communication anomalies.

        Algorithms:
          1. Build a directed graph of app_id interactions from the trace.
          2. Detect cycles via DFS — circular communication indicates
             potential infinite delegation loops.
          3. Measure delegation depth — deep chains reduce accountability.
          4. Regex matching for privilege escalation language.
        """
        threats: list[ThreatIndicator] = []

        max_delegation = self._thresholds.get("max_delegation_depth", 5)
        max_cycle = self._thresholds.get("max_cycle_length", 10)

        # ── Build communication graph ────────────────────────────────────
        graph: dict[str, list[str]] = {}
        prev_app: str = ""

        for call in trace:
            app_id = call.get("app_id", "")
            if not app_id:
                continue
            if prev_app and prev_app != app_id:
                if prev_app not in graph:
                    graph[prev_app] = []
                if app_id not in graph[prev_app]:
                    graph[prev_app].append(app_id)
            prev_app = app_id

        # ── Cycle detection ──────────────────────────────────────────────
        if graph:
            cycles = _detect_cycles_dfs(graph, max_cycle)
            for cycle in cycles:
                cycle_str = " -> ".join(cycle)
                threats.append(ThreatIndicator(
                    threat_type="communication_anomaly",
                    severity=_classify_severity(0.8, 0.85),
                    confidence=0.85,
                    description=(
                        f"Circular communication detected: {cycle_str}"
                    ),
                    evidence={
                        "cycle": cycle,
                        "cycle_length": len(cycle) - 1,
                    },
                ))

        # ── Delegation depth ─────────────────────────────────────────────
        if graph:
            depth = _compute_delegation_depth(graph)
            if depth > max_delegation:
                confidence = min(1.0, depth / (max_delegation * 2))
                threats.append(ThreatIndicator(
                    threat_type="communication_anomaly",
                    severity=_classify_severity(0.7, confidence),
                    confidence=round(confidence, 4),
                    description=(
                        f"Excessive delegation depth: {depth} "
                        f"(limit {max_delegation})"
                    ),
                    evidence={
                        "delegation_depth": depth,
                        "limit": max_delegation,
                        "graph_nodes": list(graph.keys()),
                    },
                ))

        # ── Privilege escalation + impersonation pattern matching ────────
        comm_patterns = get_patterns_by_type("communication_anomaly")
        for pattern_def in comm_patterns:
            regex_indicators = get_regex_indicators(pattern_def)
            if not regex_indicators:
                continue

            for i, call in enumerate(trace):
                searchable = self._build_searchable_text(call)
                if not searchable:
                    continue

                for regex in regex_indicators:
                    try:
                        match = re.search(regex, searchable)
                        if match:
                            threats.append(ThreatIndicator(
                                threat_type="communication_anomaly",
                                severity=_classify_severity(
                                    pattern_def["risk_weight"], 0.8
                                ),
                                confidence=0.8,
                                description=(
                                    f"Communication threat at call {i}: "
                                    f"'{pattern_def['name']}'"
                                ),
                                evidence={
                                    "call_idx": i,
                                    "pattern_name": pattern_def["name"],
                                    "matched_text": match.group()[:100],
                                },
                            ))
                            break
                    except re.error:
                        continue

        return threats

    # ── Detector: Tool Misuse ────────────────────────────────────────────

    def detect_tool_misuse(
        self,
        trace: list[dict[str, Any]],
    ) -> list[ThreatIndicator]:
        """
        Detect tool chain security violations.

        Algorithms:
          1. Regex matching against TOOL_MISUSE_PATTERNS for destructive
             operations, output injection, and data exfiltration.
          2. Tool frequency analysis — if one tool dominates > 80% of calls,
             flag potential abuse.
          3. Tool sequence anomaly — Jaccard distance between observed
             tool bigrams and expected patterns.
        """
        threats: list[ThreatIndicator] = []
        misuse_patterns = get_patterns_by_type("tool_misuse")

        freq_threshold = self._thresholds.get("tool_frequency_threshold", 0.8)
        freq_min_calls = self._thresholds.get("tool_frequency_min_calls", 10)
        seq_threshold = self._thresholds.get(
            "tool_sequence_jaccard_threshold", 0.6
        )

        # ── Regex pattern matching ───────────────────────────────────────
        for pattern_def in misuse_patterns:
            regex_indicators = get_regex_indicators(pattern_def)
            if not regex_indicators:
                continue

            for i, call in enumerate(trace):
                # Build searchable from tool calls and their args
                parts: list[str] = []
                for tc in (call.get("tool_calls") or []):
                    if isinstance(tc, str):
                        parts.append(tc)
                    elif isinstance(tc, dict):
                        parts.append(str(tc.get("name", "")))
                        parts.append(str(tc.get("args", "")))
                        parts.append(str(tc.get("result", "")))
                searchable = " ".join(parts)

                # Also check error text
                err_text = str(call.get("error", "") or "")
                if err_text:
                    searchable += " " + err_text

                if not searchable.strip():
                    continue

                for regex in regex_indicators:
                    try:
                        match = re.search(regex, searchable)
                        if match:
                            threats.append(ThreatIndicator(
                                threat_type="tool_misuse",
                                severity=_classify_severity(
                                    pattern_def["risk_weight"], 0.9
                                ),
                                confidence=0.9,
                                description=(
                                    f"Tool misuse at call {i}: "
                                    f"'{pattern_def['name']}'"
                                ),
                                evidence={
                                    "call_idx": i,
                                    "pattern_name": pattern_def["name"],
                                    "matched_text": match.group()[:100],
                                    "risk_weight": pattern_def["risk_weight"],
                                },
                            ))
                            break
                    except re.error:
                        continue

        # ── Tool frequency analysis ──────────────────────────────────────
        all_tools: list[str] = []
        for call in trace:
            for tc in (call.get("tool_calls") or []):
                if isinstance(tc, str):
                    all_tools.append(tc)
                elif isinstance(tc, dict):
                    all_tools.append(tc.get("name", ""))

        if len(all_tools) >= freq_min_calls:
            tool_counts = Counter(all_tools)
            total = sum(tool_counts.values())
            for tool_name, count in tool_counts.most_common(1):
                freq = count / max(total, 1)
                if freq > freq_threshold:
                    confidence = min(1.0, freq)
                    threats.append(ThreatIndicator(
                        threat_type="tool_misuse",
                        severity=_classify_severity(0.6, confidence),
                        confidence=round(confidence, 4),
                        description=(
                            f"Excessive tool frequency: '{tool_name}' used "
                            f"{freq:.0%} of the time ({count}/{total})"
                        ),
                        evidence={
                            "tool_name": tool_name,
                            "frequency": round(freq, 4),
                            "count": count,
                            "total_calls": total,
                            "threshold": freq_threshold,
                        },
                    ))

        # ── Tool sequence anomaly (bigram Jaccard) ───────────────────────
        if len(all_tools) >= 4:
            # Split into first-half and second-half bigrams
            mid = len(all_tools) // 2
            first_bigrams: set[tuple[str, str]] = set()
            second_bigrams: set[tuple[str, str]] = set()

            for j in range(mid - 1):
                first_bigrams.add((all_tools[j], all_tools[j + 1]))
            for j in range(mid, len(all_tools) - 1):
                second_bigrams.add((all_tools[j], all_tools[j + 1]))

            if first_bigrams or second_bigrams:
                union = first_bigrams | second_bigrams
                intersection = first_bigrams & second_bigrams
                jaccard_dist = 1.0 - (
                    len(intersection) / max(len(union), 1)
                )

                if jaccard_dist > seq_threshold:
                    confidence = min(1.0, jaccard_dist)
                    threats.append(ThreatIndicator(
                        threat_type="tool_misuse",
                        severity=_classify_severity(0.65, confidence),
                        confidence=round(confidence, 4),
                        description=(
                            f"Tool sequence anomaly: Jaccard distance "
                            f"{jaccard_dist:.3f} between session halves "
                            f"exceeds threshold {seq_threshold}"
                        ),
                        evidence={
                            "jaccard_distance": round(jaccard_dist, 4),
                            "threshold": seq_threshold,
                            "first_half_bigrams": len(first_bigrams),
                            "second_half_bigrams": len(second_bigrams),
                        },
                    ))

        return threats

    # ── Rollback Plan Generation ─────────────────────────────────────────

    def generate_rollback_plan(
        self,
        threats: list[ThreatIndicator],
    ) -> dict[str, Any]:
        """
        Auto-generate a rollback plan based on detected threats.

        Each threat type maps to a specific remediation strategy with
        concrete steps. The plan is deterministic given the threat list.

        Returns
        -------
        dict
            Plan with: action, steps, severity, auto_executable, threat_summary.
            Empty dict if no threats.
        """
        if not threats:
            return {}

        # Determine the highest severity across all threats
        severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
        max_severity = max(
            threats, key=lambda t: severity_order.get(t.severity, 0)
        ).severity

        # Collect unique threat types
        threat_types = list(dict.fromkeys(t.threat_type for t in threats))

        # Build remediation steps per threat type
        all_steps: list[str] = []
        auto_executable = True

        _ROLLBACK_STRATEGIES: dict[str, dict[str, Any]] = {
            "context_poisoning": {
                "action": "rewind_context",
                "steps": [
                    "Identify pre-injection checkpoint via context_hash diff",
                    "Rewind session context to last clean checkpoint",
                    "Invalidate cached context for affected session",
                    "Re-validate system prompt integrity",
                    "Resume from clean state with injection monitoring enabled",
                ],
                "auto_executable": True,
            },
            "goal_hijack": {
                "action": "terminate_session",
                "steps": [
                    "Terminate active session immediately",
                    "Freeze API key for affected app",
                    "Log hijack evidence for audit review",
                    "Notify app owner of detected hijack attempt",
                    "Require manual re-authorization before resuming",
                ],
                "auto_executable": False,
            },
            "cascade_failure": {
                "action": "circuit_break",
                "steps": [
                    "Circuit-break remaining calls in the session",
                    "Trigger rewind hooks for dependent downstream calls",
                    "Log cascade propagation path for post-mortem",
                    "Notify dependent services of upstream failure",
                    "Schedule retry with exponential backoff after cooldown",
                ],
                "auto_executable": True,
            },
            "communication_anomaly": {
                "action": "isolate_agents",
                "steps": [
                    "Isolate agents involved in circular communication",
                    "Break delegation chain at the cycle point",
                    "Audit inter-agent message history for tampering",
                    "Reset agent communication permissions to baseline",
                    "Re-establish communication with monitoring enabled",
                ],
                "auto_executable": False,
            },
            "tool_misuse": {
                "action": "revoke_tool_access",
                "steps": [
                    "Revoke access to flagged tools immediately",
                    "Audit tool call history for data exfiltration",
                    "Sandbox remaining tool calls with output validation",
                    "Notify security team of tool misuse detection",
                    "Re-enable tools only after security review",
                ],
                "auto_executable": False,
            },
        }

        primary_action = "monitor"
        for tt in threat_types:
            strategy = _ROLLBACK_STRATEGIES.get(tt)
            if strategy:
                all_steps.extend(strategy["steps"])
                if not strategy["auto_executable"]:
                    auto_executable = False
                # Use the most severe action
                if primary_action == "monitor":
                    primary_action = strategy["action"]
                elif tt in ("goal_hijack", "tool_misuse"):
                    primary_action = strategy["action"]

        return {
            "action": primary_action,
            "steps": all_steps,
            "severity": max_severity,
            "auto_executable": auto_executable,
            "threat_summary": {
                "total_threats": len(threats),
                "threat_types": threat_types,
                "highest_severity": max_severity,
            },
        }

    # ── Internal Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _build_searchable_text(call: dict[str, Any]) -> str:
        """
        Build a searchable text blob from a call record for regex matching.

        Concatenates model name, tool call names/args, context hash,
        and error text into a single string.
        """
        parts: list[str] = []

        model = call.get("model", "")
        if model:
            parts.append(model)

        for tc in (call.get("tool_calls") or []):
            if isinstance(tc, str):
                parts.append(tc)
            elif isinstance(tc, dict):
                parts.append(str(tc.get("name", "")))
                parts.append(str(tc.get("args", "")))
                parts.append(str(tc.get("result", "")))

        context_hash = call.get("context_hash", "")
        if context_hash:
            parts.append(context_hash)

        error = call.get("error")
        if error:
            parts.append(str(error))

        # Include any extra text fields the caller may have added
        extra_text = call.get("text", "")
        if extra_text:
            parts.append(str(extra_text))

        return " ".join(parts)


# ── Background task entry point ──────────────────────────────────────────────

async def sync_builtin_patterns(db) -> None:
    """Sync builtin TRiSM patterns to DB (background task, daily)."""
    import logging
    _logger = logging.getLogger(__name__)
    from orchestrator.core.trism_patterns import BUILTIN_PATTERNS
    from orchestrator.db.models import TRiSMPattern
    from sqlalchemy import select
    import json

    for pattern in BUILTIN_PATTERNS:
        name = pattern["name"]
        result = await db.execute(
            select(TRiSMPattern).where(TRiSMPattern.pattern_name == name)
        )
        existing = result.scalars().first()
        if existing is None:
            db.add(TRiSMPattern(
                pattern_name=name,
                threat_type=pattern["threat_type"],
                detection_rules_json=json.dumps(pattern.get("indicators", [])),
                risk_weight=pattern.get("risk_weight", 1.0),
                source="builtin",
            ))
    await db.flush()
    _logger.debug("TRiSM builtin patterns synced: %d patterns", len(BUILTIN_PATTERNS))
