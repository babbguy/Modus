# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0

"""
Modus — TRiSM Threat Pattern Definitions (Phase 8c)
=======================================================
OWASP 2026 agentic threat pattern definitions for multi-agent AI systems.

Defines pattern categories for detecting:
  - Context poisoning / memory injection
  - Goal hijacking / intent drift
  - Cascade failure prediction
  - Multi-agent communication anomalies
  - Tool chain misuse

Complies with the Four Laws:
    - Pure Python, stdlib only — zero external dependencies
    - All pattern matching < 1ms
    - Thread-safe — immutable module-level data structures
    - Zero network calls, zero disk I/O at import time
"""

from __future__ import annotations

from typing import Any


# ── Context Poisoning Patterns ───────────────────────────────────────────────
# Detect memory/context injection attacks in agentic sessions.

CONTEXT_POISONING_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "repeated_phrase_injection",
        "threat_type": "context_poisoning",
        "indicators": [
            r"(.{20,})\1{2,}",                      # Same 20+ char phrase repeated 3+ times
            r"(?i)(you must|you should|always)\s.*\1", # Repeated imperative instructions
        ],
        "risk_weight": 0.8,
        "description": (
            "Detects repeated injection of specific phrases into context, "
            "a hallmark of prompt injection attempting to override system instructions."
        ),
    },
    {
        "name": "anomalous_context_growth",
        "threat_type": "context_poisoning",
        "indicators": [
            r"__context_growth_rate_check__",  # Sentinel — evaluated by engine, not regex
        ],
        "risk_weight": 0.7,
        "description": (
            "Flags sessions where context size grows faster than 2x the rolling "
            "average between consecutive calls, indicating bulk injection."
        ),
    },
    {
        "name": "entropy_spike",
        "threat_type": "context_poisoning",
        "indicators": [
            r"__entropy_spike_check__",  # Sentinel — evaluated by engine, not regex
        ],
        "risk_weight": 0.75,
        "description": (
            "Detects sudden Shannon entropy spikes in context diffs between "
            "consecutive calls. Default threshold: 3.5 bits/token. High entropy "
            "in a context delta often indicates injected adversarial content."
        ),
    },
    {
        "name": "known_injection_signatures",
        "threat_type": "context_poisoning",
        "indicators": [
            r"(?i)ignore\s+(all\s+)?previous\s+instructions",
            r"(?i)disregard\s+(all\s+)?(prior|previous|above)",
            r"(?i)new\s+system\s+prompt\s*[:=]",
            r"(?i)you\s+are\s+now\s+(a|an)\s+",
            r"(?i)\[SYSTEM\]\s*override",
            r"(?i)<<<\s*END\s*SYSTEM\s*>>>",
            r"(?i)forget\s+(everything|all)\s+(you|that)",
            r"(?i)pretend\s+(you\s+are|to\s+be)",
            r"(?i)act\s+as\s+(if|though)\s+your\s+instructions",
            r"(?i)from\s+now\s+on,?\s+(you|your)\s+(are|will|must)",
        ],
        "risk_weight": 0.95,
        "description": (
            "Matches known prompt injection signatures targeting system prompt "
            "overrides. These are well-documented attack vectors from OWASP "
            "LLM Top 10 and agentic threat research."
        ),
    },
    {
        "name": "base64_payload_injection",
        "threat_type": "context_poisoning",
        "indicators": [
            r"[A-Za-z0-9+/]{100,}={0,2}",  # Large base64 blob in context
            r"(?i)eval\s*\(\s*atob\s*\(",    # JS-style base64 decode + eval
        ],
        "risk_weight": 0.85,
        "description": (
            "Detects large base64-encoded payloads injected into context, "
            "potentially carrying obfuscated instructions or exfiltration vectors."
        ),
    },
]


# ── Goal Hijack Patterns ─────────────────────────────────────────────────────
# Detect intent drift and session takeover in agentic workflows.

GOAL_HIJACK_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "tool_call_pattern_change",
        "threat_type": "goal_hijack",
        "indicators": [
            r"__tool_pattern_shift_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.7,
        "description": (
            "Detects sudden shifts in tool call patterns mid-session. "
            "If the set of tools used in the last N calls differs significantly "
            "from the first N calls, it may indicate goal hijacking."
        ),
    },
    {
        "name": "model_escalation",
        "threat_type": "goal_hijack",
        "indicators": [
            r"__model_escalation_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.6,
        "description": (
            "Flags unexpected model escalation mid-session (e.g., haiku to opus, "
            "gpt-3.5 to gpt-4). Legitimate escalation is explicit; hijacked "
            "sessions may escalate to extract more capable outputs."
        ),
    },
    {
        "name": "intent_vector_rotation",
        "threat_type": "goal_hijack",
        "indicators": [
            r"__intent_rotation_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.8,
        "description": (
            "Measures cosine distance between consecutive call intent vectors. "
            "Threshold: > 0.7 indicates a drastic intent change that may "
            "signal goal hijacking by an injected instruction."
        ),
    },
    {
        "name": "known_hijack_sequences",
        "threat_type": "goal_hijack",
        "indicators": [
            r"(?i)actually,?\s+(let'?s|we\s+should|change|switch)\s+(to|the)",
            r"(?i)new\s+(objective|goal|task|mission)\s*[:=]",
            r"(?i)priority\s+override\s*[:=]",
            r"(?i)urgent\s*[:!]\s*(change|update|switch)",
            r"(?i)the\s+real\s+(task|goal|objective)\s+is",
        ],
        "risk_weight": 0.75,
        "description": (
            "Matches known goal hijack phrasings that attempt to redirect "
            "an agent mid-session by injecting new objectives."
        ),
    },
    {
        "name": "output_format_shift",
        "threat_type": "goal_hijack",
        "indicators": [
            r"__output_format_shift_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.5,
        "description": (
            "Detects sudden changes in output format (e.g., structured JSON "
            "to freeform text) which may indicate the agent has been redirected "
            "to serve a different objective."
        ),
    },
]


# ── Cascade Failure Patterns ─────────────────────────────────────────────────
# Predict and detect cascading failures in multi-step agent workflows.

CASCADE_FAILURE_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "error_propagation_rate",
        "threat_type": "cascade_failure",
        "indicators": [
            r"__error_propagation_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.85,
        "description": (
            "Tracks error rate across consecutive calls. If errors exceed "
            "the threshold (default: 3 consecutive errors or > 40% error rate "
            "in a sliding window), predicts cascade failure."
        ),
    },
    {
        "name": "dependency_chain_depth",
        "threat_type": "cascade_failure",
        "indicators": [
            r"__dependency_depth_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.7,
        "description": (
            "Monitors dependency chain depth in tool call sequences. "
            "Chains exceeding the configured limit (default: 8) are flagged "
            "as cascade-vulnerable due to deep coupling."
        ),
    },
    {
        "name": "circular_dependency",
        "threat_type": "cascade_failure",
        "indicators": [
            r"__circular_dependency_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.9,
        "description": (
            "Detects circular dependencies in tool call chains where "
            "output of tool A feeds tool B which feeds tool A. "
            "Circular chains guarantee cascade on any single failure."
        ),
    },
    {
        "name": "resource_exhaustion_pattern",
        "threat_type": "cascade_failure",
        "indicators": [
            r"(?i)(rate\s*limit|429|too\s*many\s*requests)",
            r"(?i)(timeout|timed?\s*out|deadline\s*exceeded)",
            r"(?i)(out\s*of\s*memory|oom|memory\s*error)",
            r"(?i)(quota\s*exceeded|insufficient\s*quota)",
        ],
        "risk_weight": 0.8,
        "description": (
            "Matches error messages indicating resource exhaustion that "
            "commonly precede cascade failures in multi-agent systems."
        ),
    },
]


# ── Communication Anomaly Patterns ───────────────────────────────────────────
# Detect threats in multi-agent communication topologies.

COMMUNICATION_ANOMALY_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "circular_communication",
        "threat_type": "communication_anomaly",
        "indicators": [
            r"__circular_comm_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.8,
        "description": (
            "Detects circular communication patterns (A -> B -> C -> A) "
            "in multi-agent traces. Cycles can cause infinite delegation "
            "loops and resource exhaustion."
        ),
    },
    {
        "name": "excessive_delegation_depth",
        "threat_type": "communication_anomaly",
        "indicators": [
            r"__delegation_depth_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.7,
        "description": (
            "Flags delegation chains exceeding the configured depth limit "
            "(default: 5). Deep delegation reduces accountability and "
            "increases latency and failure probability."
        ),
    },
    {
        "name": "privilege_escalation",
        "threat_type": "communication_anomaly",
        "indicators": [
            r"(?i)(admin|root|superuser|elevated)\s*(access|privilege|permission)",
            r"(?i)(escalat|promot|elevat)\w*\s*(privilege|permission|access|role)",
            r"(?i)grant\s+(all|admin|superuser|root)",
            r"(?i)sudo\s+",
            r"(?i)as\s+(admin|root|superuser)",
        ],
        "risk_weight": 0.9,
        "description": (
            "Detects language patterns associated with privilege escalation "
            "attempts in agent-to-agent communication."
        ),
    },
    {
        "name": "agent_impersonation",
        "threat_type": "communication_anomaly",
        "indicators": [
            r"(?i)i\s+am\s+(the\s+)?(system|admin|orchestrator|supervisor)",
            r"(?i)speaking\s+as\s+(the\s+)?(system|admin|orchestrator)",
            r"(?i)this\s+is\s+(the\s+)?(system|control|admin)\s+(agent|process)",
        ],
        "risk_weight": 0.85,
        "description": (
            "Detects attempts by one agent to impersonate a higher-privilege "
            "agent or system process in multi-agent communication."
        ),
    },
]


# ── Tool Misuse Patterns ─────────────────────────────────────────────────────
# Detect tool chain security violations and dangerous tool usage.

TOOL_MISUSE_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "destructive_operations",
        "threat_type": "tool_misuse",
        "indicators": [
            r"(?i)(drop|truncate|delete\s+from|alter\s+table)\s+",
            r"(?i)(rm\s+-rf|rmdir|del\s+/[sfq])",
            r"(?i)(format\s+[a-z]:)",
            r"(?i)(shutdown|reboot|halt|poweroff)",
            r"(?i)(curl|wget)\s+.*\|\s*(bash|sh|python|exec)",
        ],
        "risk_weight": 0.95,
        "description": (
            "Matches tool calls or arguments containing destructive operations "
            "such as database drops, recursive file deletion, or remote code execution."
        ),
    },
    {
        "name": "unusual_tool_sequencing",
        "threat_type": "tool_misuse",
        "indicators": [
            r"__tool_sequence_anomaly_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.65,
        "description": (
            "Detects unusual tool call ordering that deviates from established "
            "session patterns. Uses Jaccard distance between expected and "
            "observed tool bigrams."
        ),
    },
    {
        "name": "tool_output_injection",
        "threat_type": "tool_misuse",
        "indicators": [
            r"(?i)<\s*script\s*>",
            r"(?i)javascript\s*:",
            r"(?i)on(error|load|click)\s*=",
            r"\{\{.*\}\}",                          # Template injection
            r"(?i)\$\{.*\}",                         # Expression injection
            r"(?i)(;|&&|\|\|)\s*(cat|ls|whoami|id|env|printenv)",  # Command injection
        ],
        "risk_weight": 0.9,
        "description": (
            "Detects injection signatures in tool outputs that could be used "
            "to inject code, scripts, or commands into downstream processing."
        ),
    },
    {
        "name": "data_exfiltration_attempt",
        "threat_type": "tool_misuse",
        "indicators": [
            r"(?i)(curl|wget|fetch|http)\s+.*\b(password|secret|token|key|cred)",
            r"(?i)(send|post|upload|transmit)\s+.*(password|secret|api.?key|token)",
            r"(?i)base64\s*.*\b(password|secret|key|token)",
            r"(?i)(webhook|callback|exfil)\s*[:=]?\s*https?://",
        ],
        "risk_weight": 0.95,
        "description": (
            "Detects patterns consistent with data exfiltration attempts, "
            "including sending credentials to external URLs or encoding "
            "secrets for transmission."
        ),
    },
    {
        "name": "excessive_tool_frequency",
        "threat_type": "tool_misuse",
        "indicators": [
            r"__tool_frequency_check__",  # Sentinel — evaluated by engine
        ],
        "risk_weight": 0.6,
        "description": (
            "Flags sessions where a single tool is called at an abnormally "
            "high frequency, potentially indicating automated abuse or "
            "denial-of-service via tool exhaustion."
        ),
    },
]


# ── Aggregated Pattern List ──────────────────────────────────────────────────

BUILTIN_PATTERNS: list[dict[str, Any]] = (
    CONTEXT_POISONING_PATTERNS
    + GOAL_HIJACK_PATTERNS
    + CASCADE_FAILURE_PATTERNS
    + COMMUNICATION_ANOMALY_PATTERNS
    + TOOL_MISUSE_PATTERNS
)
"""Flat list of all built-in TRiSM threat patterns."""


# ── Default Thresholds ───────────────────────────────────────────────────────

DEFAULT_THRESHOLDS: dict[str, Any] = {
    # Context poisoning
    "entropy_spike_threshold": 3.5,          # bits/token — above this is suspicious
    "context_growth_rate_max": 2.0,          # max ratio vs rolling avg
    "context_growth_window": 5,              # calls to look back for rolling avg
    "min_context_length_for_entropy": 50,    # chars — skip entropy calc on tiny contexts

    # Goal hijack
    "intent_cosine_distance_threshold": 0.7, # above this = drastic intent change
    "tool_pattern_shift_window": 5,          # calls to compare early vs late
    "tool_pattern_shift_threshold": 0.6,     # Jaccard distance for tool set shift
    "model_escalation_tiers": {              # tier ordering for escalation detection
        "haiku": 1, "sonnet": 2, "opus": 3,
        "gpt-3.5-turbo": 1, "gpt-4-turbo": 2, "gpt-4": 2, "gpt-4o": 2,
        "gpt-4o-mini": 1,
        "claude-3-haiku": 1, "claude-3-sonnet": 2, "claude-3-opus": 3,
        "claude-3.5-haiku": 1, "claude-3.5-sonnet": 2,
        "claude-4-haiku": 1, "claude-4-sonnet": 2, "claude-4-opus": 3,
        "gemini-flash": 1, "gemini-pro": 2, "gemini-ultra": 3,
    },

    # Cascade failure
    "consecutive_error_threshold": 3,        # consecutive errors before cascade alert
    "error_rate_threshold": 0.4,             # fraction of calls in window that errored
    "error_rate_window": 10,                 # sliding window size for error rate
    "dependency_chain_depth_limit": 8,       # max acceptable chain depth
    "cascade_monte_carlo_runs": 100,         # simulation runs for cascade prediction

    # Communication anomaly
    "max_delegation_depth": 5,               # max A->B->C->... depth
    "max_cycle_length": 10,                  # max cycle length to search for

    # Tool misuse
    "tool_frequency_threshold": 0.8,         # fraction — if one tool > 80% of calls
    "tool_frequency_min_calls": 10,          # minimum calls before frequency check
    "tool_sequence_jaccard_threshold": 0.6,  # Jaccard distance for anomaly detection

    # Global
    "risk_score_critical_threshold": 0.85,   # above this = block/alert
    "risk_score_warning_threshold": 0.5,     # above this = warn
    "max_threats_before_block": 3,           # cumulative threats before auto-block
    "scan_timeout_ms": 50,                   # max time for a full scan (soft limit)
}
"""
Configurable thresholds for TRiSM threat detection. All values have safe
defaults that balance false-positive rate against detection sensitivity.
Customers can override via policy configuration.
"""


# ── Model Tier Helpers ───────────────────────────────────────────────────────

def get_model_tier(model_name: str, tiers: dict[str, int] | None = None) -> int:
    """
    Return the capability tier for a model name (1=low, 2=mid, 3=high).

    Performs case-insensitive substring matching against the tier map.
    Returns 0 if the model is not recognized.
    """
    if tiers is None:
        tiers = DEFAULT_THRESHOLDS["model_escalation_tiers"]
    model_lower = model_name.lower()
    for pattern, tier in tiers.items():
        if pattern in model_lower:
            return tier
    return 0


def get_patterns_by_type(threat_type: str) -> list[dict[str, Any]]:
    """Return all builtin patterns matching the given threat_type."""
    return [p for p in BUILTIN_PATTERNS if p["threat_type"] == threat_type]


def get_regex_indicators(pattern: dict[str, Any]) -> list[str]:
    """
    Return only the regex indicators from a pattern, filtering out
    sentinel placeholders (those starting with '__' and ending with '__').
    """
    return [
        ind for ind in pattern.get("indicators", [])
        if not (ind.startswith("__") and ind.endswith("__"))
    ]
