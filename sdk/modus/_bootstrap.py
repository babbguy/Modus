"""
modus._bootstrap
=========================
Runs automatically when `import modus` is executed.
Starts the agent if credentials are available; warns and exits cleanly if not.
Never crashes — the monitored application must always be able to start.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("modus")

_agent = None


def _safe_int(env_var: str, default: int) -> int:
    raw = os.getenv(env_var)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Modus: invalid integer for %s=%r, using default %d", env_var, raw, default)
        return default


def _safe_float(env_var: str, default: float) -> float:
    raw = os.getenv(env_var)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning("Modus: invalid float for %s=%r, using default %s", env_var, raw, default)
        return default


def _start() -> None:
    global _agent

    # Explicit opt-out — useful for test environments and CI.
    # Set MODUS_DISABLED=true to suppress all startup warnings.
    if os.getenv("MODUS_DISABLED", "").lower() in ("true", "1", "yes"):
        logger.debug("Modus disabled via MODUS_DISABLED.")
        return

    url = os.getenv("MODUS_URL", os.getenv("MODUS_ORCHESTRATOR_URL", ""))
    token = os.getenv("MODUS_TEAM_TOKEN", "")

    if not url or not token:
        missing = []
        if not url:
            missing.append("MODUS_URL")
        if not token:
            missing.append("MODUS_TEAM_TOKEN")
        logger.warning(
            "Modus not started — set %s to enable AI cost governance.",
            " and ".join(missing),
        )
        return

    try:
        from modus.agent import ModusAgent
        _agent = ModusAgent(
            orchestrator_url=url,
            team_token=token,
            app_id=os.getenv("MODUS_APP_ID"),
            app_name=os.getenv("MODUS_APP_NAME"),
            environment=os.getenv("MODUS_ENVIRONMENT", "production"),
            fail_open=os.getenv("MODUS_FAIL_OPEN", "true").lower() != "false",
            flush_interval=_safe_int("MODUS_FLUSH_INTERVAL", 30),
            evaluate_cache_ttl=_safe_float("MODUS_EVALUATE_CACHE_TTL", 2.0),
            timeout=_safe_float("MODUS_TIMEOUT", 3.0),
            max_buffer_size=_safe_int("MODUS_MAX_BUFFER_SIZE", 10000),
        ).start()

    except Exception as exc:
        # Log and continue — never crash the application
        logger.error(
            "Modus failed to start (application will continue without governance): %s",
            exc,
            exc_info=True,
        )


def get_agent():
    """Return the running agent instance, or None if not started."""
    return _agent


_start()
