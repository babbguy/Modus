"""
Modus SDK — Rewind Engine
=========================

SDK-side "rewind" hooks for post-call anomaly rollback + failure
context capture for experience distillation.

When the SDK detects a post-call anomaly (circuit breaker trip, output
validation failure, budget suspension mid-session), it triggers
registered rewind hooks in LIFO order and captures the failure context.

The failure context is POSTed to the orchestrator's governance API for
experience distillation — the governance loop analyzes patterns and
proposes preventive policies.

Thread-safe. Zero overhead when no hooks registered.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger("modus.rewind")


@dataclass
class RewindAction:
    """Result of a single rewind hook execution."""
    tool_name: str
    success: bool
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class RewindContext:
    """Context passed to rewind hooks and captured for distillation."""
    trigger_reason: str  # circuit_breaker, output_validation, budget_suspension, manual
    session_id: Optional[str] = None
    call_sequence: List[Dict[str, Any]] = field(default_factory=list)
    error_detail: Optional[str] = None
    tokens_consumed: int = 0
    cost_consumed: float = 0.0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class RewindRegistry:
    """
    Registry of rewind hooks for tool rollback.

    Usage::

        agent = ModusAgent(...)
        agent.register_rewind_hook("database",
            lambda ctx: db.rollback(ctx["savepoint"]))
        agent.register_rewind_hook("file_system",
            lambda ctx: os.unlink(ctx["temp_file"]))

    On trigger (circuit breaker, output validation fail, budget suspension):
    hooks are called in LIFO order (last registered = first rolled back).
    """

    def __init__(self) -> None:
        self._hooks: Dict[str, Callable] = {}
        self._hook_order: List[str] = []  # insertion order for LIFO
        self._lock = threading.Lock()
        self._rewind_history: List[Dict[str, Any]] = []
        self._max_history = 100

    def register(self, tool_name: str, rollback_fn: Callable) -> None:
        """
        Register a rewind hook for a tool.

        Args:
            tool_name: Unique identifier for the tool (e.g., "database", "file_system")
            rollback_fn: Callable that accepts a dict context and returns bool (success).
                         Must be idempotent and safe to call multiple times.
        """
        with self._lock:
            if tool_name not in self._hooks:
                self._hook_order.append(tool_name)
            self._hooks[tool_name] = rollback_fn

    def unregister(self, tool_name: str) -> None:
        """Remove a rewind hook."""
        with self._lock:
            self._hooks.pop(tool_name, None)
            if tool_name in self._hook_order:
                self._hook_order.remove(tool_name)

    @property
    def has_hooks(self) -> bool:
        """Check if any hooks are registered. O(1), no lock needed."""
        return bool(self._hooks)

    def trigger(self, context: RewindContext) -> List[RewindAction]:
        """
        Execute all registered rewind hooks in LIFO order.

        Returns list of RewindAction results.
        Thread-safe: takes snapshot of hooks under lock, then releases
        lock before executing (hooks may be slow).
        """
        with self._lock:
            # Snapshot hooks in reverse order (LIFO)
            hooks_snapshot = [
                (name, self._hooks[name])
                for name in reversed(self._hook_order)
                if name in self._hooks
            ]

        if not hooks_snapshot:
            return []

        actions: List[RewindAction] = []

        for tool_name, rollback_fn in hooks_snapshot:
            start = time.perf_counter()
            try:
                # Build context dict for the hook
                hook_ctx = {
                    "trigger_reason": context.trigger_reason,
                    "session_id": context.session_id,
                    "error_detail": context.error_detail,
                    "tokens_consumed": context.tokens_consumed,
                    "cost_consumed": context.cost_consumed,
                    "call_sequence": context.call_sequence,
                }
                result = rollback_fn(hook_ctx)
                elapsed = (time.perf_counter() - start) * 1000
                actions.append(RewindAction(
                    tool_name=tool_name,
                    success=bool(result),
                    duration_ms=elapsed,
                ))
            except Exception as exc:
                elapsed = (time.perf_counter() - start) * 1000
                actions.append(RewindAction(
                    tool_name=tool_name,
                    success=False,
                    error=str(exc)[:500],
                    duration_ms=elapsed,
                ))
                logger.warning(
                    "Rewind hook '%s' failed: %s", tool_name, exc,
                    exc_info=False,
                )

        # Store in history
        with self._lock:
            entry = {
                "timestamp": context.timestamp,
                "trigger_reason": context.trigger_reason,
                "session_id": context.session_id,
                "actions": [
                    {"tool": a.tool_name, "success": a.success,
                     "error": a.error, "duration_ms": round(a.duration_ms, 2)}
                    for a in actions
                ],
            }
            if len(self._rewind_history) >= self._max_history:
                self._rewind_history.pop(0)
            self._rewind_history.append(entry)

        return actions

    @property
    def history(self) -> List[Dict[str, Any]]:
        """Return rewind event history (most recent last)."""
        with self._lock:
            return list(self._rewind_history)


def report_rewind_event(
    orchestrator_url: str,
    api_key: str,
    app_id: str,
    team_id: str,
    context: RewindContext,
    actions: List[RewindAction],
    timeout: float = 5.0,
) -> bool:
    """
    POST rewind event to orchestrator for experience distillation.
    Fire-and-forget — failures are logged, never raised.

    Returns True if successfully posted, False otherwise.
    """
    if not orchestrator_url:
        return False

    payload = json.dumps({
        "session_id": context.session_id or "",
        "app_id": app_id,
        "team_id": team_id,
        "trigger_reason": context.trigger_reason,
        "actions_rolled_back": [
            {"tool_name": a.tool_name, "success": a.success,
             "error": a.error, "duration_ms": round(a.duration_ms, 2)}
            for a in actions
        ],
        "failure_context": {
            "error_detail": context.error_detail,
            "tokens_consumed": context.tokens_consumed,
            "cost_consumed": context.cost_consumed,
            "call_sequence_length": len(context.call_sequence),
        },
    }).encode()

    url = f"{orchestrator_url.rstrip('/')}/api/v1/governance/rewind-event"
    req = urllib_request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200 or resp.status == 201
    except (urllib_error.URLError, OSError, TimeoutError) as exc:
        logger.debug("Failed to report rewind event: %s", exc)
        return False
