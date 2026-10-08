"""
Modus — External Connection Checker
=============================================
Gathers and tests all external connections from a Modus instance.

Uses stdlib only (urllib.request) for HTTP checks. All checks have 3-second
timeouts by default. Results are cached in memory with a 60-second TTL.

Connection categories:
    platform      — Nomus, Federation
    ai_provider   — Anthropic, OpenAI, Google, Azure, Ollama
    notification  — Slack, Teams, SMTP, PagerDuty, custom webhooks
    integration   — Gateway proxy upstream endpoints
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_DEFAULT_TIMEOUT = 3  # seconds
_CACHE_TTL = 60       # seconds

# Known AI provider base URLs
_AI_PROVIDER_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "google": "https://generativelanguage.googleapis.com",
    "deepseek": "https://api.deepseek.com",
    "ollama": "http://localhost:11434",
}

# ── In-memory cache ─────────────────────────────────────────────────────────

_cache: dict[str, tuple[dict, float]] = {}  # id -> (result_dict, timestamp)


def _cache_get(connection_id: str) -> Optional[dict]:
    """Return cached result if TTL has not expired, else None."""
    entry = _cache.get(connection_id)
    if entry is None:
        return None
    result, ts = entry
    if time.monotonic() - ts > _CACHE_TTL:
        _cache.pop(connection_id, None)
        return None
    return result


def _cache_set(connection_id: str, result: dict) -> None:
    """Store result in cache with current timestamp."""
    _cache[connection_id] = (result, time.monotonic())


def clear_cache() -> None:
    """Clear all cached connection results."""
    _cache.clear()


# ── URL / Key masking ───────────────────────────────────────────────────────

def _mask_url(url: str) -> str:
    """
    Mask a URL for safe display.
    Shows scheme + first 8 chars of host + '***'.
    Example: https://api.anth*** or http://localho***
    """
    if not url:
        return ""
    try:
        # Parse manually to avoid importing urllib.parse on hot path
        # but we're cold path here, so it's fine
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        scheme = parsed.scheme or "https"
        if len(host) <= 8:
            masked_host = host + "***"
        else:
            masked_host = host[:8] + "***"
        return f"{scheme}://{masked_host}"
    except Exception:
        # Fallback: show first 12 chars
        if len(url) > 12:
            return url[:12] + "***"
        return url


def _mask_key(key: str) -> str:
    """
    Mask an API key for safe display.
    Shows first 3 chars + '...' + last 4 chars.
    Example: sk-...xY7z
    """
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "..."
    return key[:3] + "..." + key[-4:]


# ── HTTP check helper ───────────────────────────────────────────────────────

def _check_url(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    method: str = "GET",
) -> tuple[bool, int, Optional[str]]:
    """
    Perform a synchronous HTTP check against a URL.

    Returns:
        (ok, status_code, error_message)
        ok is True if we got any HTTP response (even 4xx — means the host is up).
        status_code is 0 on network/timeout errors.
    """
    try:
        req = Request(url, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        # Allow self-signed certs in dev environments
        ctx = ssl.create_default_context()
        response = urlopen(req, timeout=timeout, context=ctx)
        return (True, response.status, None)
    except HTTPError as exc:
        # HTTP error response — host is reachable
        # 401/403 means the endpoint exists but requires auth (still "connected")
        return (True, exc.code, None)
    except URLError as exc:
        reason = str(exc.reason) if exc.reason else "Connection failed"
        return (False, 0, reason)
    except TimeoutError:
        return (False, 0, "Connection timed out")
    except OSError as exc:
        return (False, 0, str(exc))
    except Exception as exc:
        return (False, 0, f"{type(exc).__name__}: {exc}")


async def _async_check_url(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    method: str = "GET",
) -> tuple[bool, int, Optional[str]]:
    """Run _check_url in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _check_url, url, headers, timeout, method
    )


# ── Connection builders ─────────────────────────────────────────────────────

def _make_connection(
    connection_id: str,
    category: str,
    name: str,
    conn_type: str,
    endpoint: str = "",
    status: str = "not_configured",
    metadata: Optional[dict[str, Any]] = None,
) -> dict:
    """Build a standardized connection dict."""
    return {
        "id": connection_id,
        "category": category,
        "name": name,
        "type": conn_type,
        "endpoint": _mask_url(endpoint) if endpoint else "",
        "status": status,
        "last_check": None,
        "last_success": None,
        "last_error": None,
        "metadata": metadata or {},
        # Internal: raw URL for testing (never sent to client)
        "_raw_url": endpoint,
        "_raw_headers": {},
    }


def _gather_ai_providers() -> list[dict]:
    """Gather AI provider connections from settings."""
    connections: list[dict] = []

    # Primary assistant provider
    provider = (settings.assistant_provider or "").lower().strip()
    if provider and settings.assistant_api_key:
        base_url = _AI_PROVIDER_URLS.get(provider, "")
        # Azure override
        if provider == "azure" and settings.summary_base_url:
            base_url = settings.summary_base_url
        conn = _make_connection(
            f"ai_{provider}", "ai_provider",
            f"{provider.title()} API", "ai_provider",
            endpoint=base_url,
            status="unknown",
            metadata={
                "provider": provider,
                "model": settings.assistant_model or "default",
                "key_preview": _mask_key(settings.assistant_api_key),
            },
        )
        # Set auth header for testing
        if provider == "anthropic":
            conn["_raw_headers"] = {
                "x-api-key": settings.assistant_api_key,
                "anthropic-version": "2023-06-01",
            }
        elif provider in ("openai", "azure", "deepseek"):
            conn["_raw_headers"] = {
                "Authorization": f"Bearer {settings.assistant_api_key}",
            }
        elif provider == "google":
            # Google uses query param, but we can still check reachability
            pass
        connections.append(conn)
    elif provider:
        connections.append(_make_connection(
            f"ai_{provider}", "ai_provider",
            f"{provider.title()} API", "ai_provider",
            status="not_configured",
            metadata={"provider": provider, "note": "API key not set"},
        ))

    # Summary agent (if different from primary)
    summary_provider = (settings.summary_agent or "").lower().strip()
    summary_key = settings.summary_api_key.get_secret_value() if settings.summary_api_key else ""
    if summary_provider and summary_provider != provider:
        base_url = settings.summary_base_url or _AI_PROVIDER_URLS.get(summary_provider, "")
        if summary_key:
            conn = _make_connection(
                f"ai_summary_{summary_provider}", "ai_provider",
                f"{summary_provider.title()} API (Summary)", "ai_provider",
                endpoint=base_url,
                status="unknown",
                metadata={
                    "provider": summary_provider,
                    "model": settings.summary_model_id or "default",
                    "role": "summary_agent",
                    "key_preview": _mask_key(summary_key),
                },
            )
            if summary_provider == "anthropic":
                conn["_raw_headers"] = {
                    "x-api-key": summary_key,
                    "anthropic-version": "2023-06-01",
                }
            elif summary_provider in ("openai", "azure", "deepseek"):
                conn["_raw_headers"] = {
                    "Authorization": f"Bearer {summary_key}",
                }
            connections.append(conn)
        else:
            connections.append(_make_connection(
                f"ai_summary_{summary_provider}", "ai_provider",
                f"{summary_provider.title()} API (Summary)", "ai_provider",
                status="not_configured",
                metadata={"provider": summary_provider, "note": "API key not set"},
            ))

    return connections


async def _gather_notifications() -> list[dict]:
    """Gather notification channel connections from the real DB config.

    Notification channels are configured via the dashboard and persisted in the
    NotificationChannelConfig row — NOT in settings. The previous code read
    settings.slack_webhook_url etc., which don't exist, so configured channels
    never appeared on the connections page.
    """
    connections: list[dict] = []

    from orchestrator.db.session import _session_factory
    if _session_factory is None:
        return connections

    try:
        from orchestrator.api.notifications import _load_config
        async with _session_factory() as db:
            cfg = await _load_config(db)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Connection checker: could not load notification config: %s", exc)
        return connections

    if not cfg:
        return connections

    slack = cfg.get("slack", {})
    if slack.get("enabled") and slack.get("webhook_url"):
        connections.append(_make_connection(
            "notify_slack", "notification", "Slack Webhook", "slack",
            endpoint=_mask_url(slack["webhook_url"]), status="configured",
        ))

    teams = cfg.get("teams", {})
    if teams.get("enabled") and teams.get("webhook_url"):
        connections.append(_make_connection(
            "notify_teams", "notification", "Teams Webhook", "teams",
            endpoint=_mask_url(teams["webhook_url"]), status="configured",
        ))

    email = cfg.get("email", {})
    if email.get("enabled") and email.get("smtp_host"):
        connections.append(_make_connection(
            "notify_smtp", "notification", "SMTP Server", "smtp",
            endpoint=email["smtp_host"], status="configured",
            metadata={"port": email.get("smtp_port", 587),
                      "recipients": len(email.get("recipients", []))},
        ))

    pd = cfg.get("pagerduty", {})
    if pd.get("enabled") and pd.get("integration_key"):
        connections.append(_make_connection(
            "notify_pagerduty", "notification", "PagerDuty", "pagerduty",
            endpoint="https://events.pagerduty.com", status="configured",
            metadata={"key_preview": _mask_key(pd["integration_key"])},
        ))

    webhook = cfg.get("webhook", {})
    if webhook.get("enabled") and webhook.get("url"):
        connections.append(_make_connection(
            "notify_custom", "notification", "Custom Webhook", "webhook",
            endpoint=_mask_url(webhook["url"]), status="configured",
        ))

    return connections


def _gather_nomus() -> list[dict]:
    """Gather Nomus regulatory engine connections.

    Nomus appears in both "platform" (core infrastructure dependency)
    and "integration" (regulatory compliance integration) categories,
    matching the dual-category pattern used for first-class integrations.
    """
    from orchestrator.core.nomus_client import (
        _cached_policies,
        _cached_state_hash,
        _last_sync_ts,
        _last_sync_error,
        _sync_count,
        _bundle_version,
    )

    url = getattr(settings, "nomus_url", "") or ""

    # Build rich metadata from cached nomus state
    last_sync_iso: Optional[str] = None
    if _last_sync_ts > 0:
        last_sync_iso = datetime.fromtimestamp(
            _last_sync_ts, tz=timezone.utc
        ).isoformat()

    policy_count = len(_cached_policies)

    # Derive jurisdictions from cached policies
    jurisdictions = sorted({
        p.get("jurisdiction", "")
        for p in _cached_policies
        if p.get("jurisdiction")
    })

    metadata: dict[str, Any] = {
        "last_sync": last_sync_iso,
        "policy_count": policy_count,
        "regulation_count": len(jurisdictions),
        "jurisdictions": jurisdictions,
        "sync_count": _sync_count,
        "version": _bundle_version,
        "state_hash": _cached_state_hash,
        "auto_sync": settings.nomus_auto_sync,
    }
    if _last_sync_error:
        metadata["last_error"] = _last_sync_error

    if not url:
        metadata["note"] = "Nomus URL not configured"
        return [_make_connection(
            "nomus", "platform", "Nomus", "nomus",
            status="not_configured",
            metadata=metadata,
        )]

    # Determine connection status from cached state
    if policy_count > 0 and not _last_sync_error:
        conn_status = "connected"
    elif _last_sync_error:
        conn_status = "error"
    else:
        conn_status = "unknown"

    # Platform connection (infrastructure dependency)
    platform_conn = _make_connection(
        "nomus", "platform", "Nomus", "nomus",
        endpoint=url, status=conn_status,
        metadata=metadata,
    )

    # Integration connection (regulatory compliance integration)
    integration_conn = _make_connection(
        "nomus_integration", "integration",
        "Nomus Regulatory Engine", "nomus",
        endpoint=url, status=conn_status,
        metadata={
            **metadata,
            "direction": "outbound",
            "protocol": "REST + Ed25519 signed bundles",
        },
    )

    return [platform_conn, integration_conn]


def _gather_federation() -> list[dict]:
    """Gather Federation connection."""
    if not settings.federation_enabled:
        return [_make_connection(
            "federation", "platform", "Federation Hub", "federation",
            status="disabled",
            metadata={"note": "Federation is disabled"},
        )]
    url = settings.federation_aggregator_url
    if not url:
        return [_make_connection(
            "federation", "platform", "Federation Hub", "federation",
            status="not_configured",
            metadata={"note": "Federation enabled but no aggregator URL set"},
        )]
    return [_make_connection(
        "federation", "platform", "Federation Hub", "federation",
        endpoint=url, status="unknown",
        metadata={
            "participation_enabled": settings.federation_participation_enabled,
            "sync_interval_hours": settings.federation_sync_interval_hours,
        },
    )]


def _gather_gateway() -> list[dict]:
    """Gather Gateway proxy upstream connections."""
    if not settings.gateway_enabled:
        return []

    connections: list[dict] = []

    if settings.gateway_openai_base_url:
        connections.append(_make_connection(
            "gateway_openai", "integration",
            "Gateway: OpenAI Upstream", "gateway",
            endpoint=settings.gateway_openai_base_url,
            status="unknown",
            metadata={"provider": "openai"},
        ))

    if settings.gateway_anthropic_base_url:
        connections.append(_make_connection(
            "gateway_anthropic", "integration",
            "Gateway: Anthropic Upstream", "gateway",
            endpoint=settings.gateway_anthropic_base_url,
            status="unknown",
            metadata={"provider": "anthropic"},
        ))

    return connections


# ── Public API ───────────────────────────────────────────────────────────────

async def gather_all_connections() -> list[dict]:
    """
    Collect all configured connections from settings.
    Returns list of connection dicts without performing active tests.
    Cached results are used if available.
    """
    connections: list[dict] = []

    # Gather from all sources
    connections.extend(_gather_ai_providers())
    connections.extend(await _gather_notifications())
    connections.extend(_gather_nomus())
    connections.extend(_gather_federation())
    connections.extend(_gather_gateway())

    # Apply cached results where available
    for conn in connections:
        cached = _cache_get(conn["id"])
        if cached:
            conn["status"] = cached["status"]
            conn["last_check"] = cached["last_check"]
            conn["last_success"] = cached["last_success"]
            conn["last_error"] = cached["last_error"]

    return connections


async def _test_single_connection(conn: dict) -> dict:
    """
    Actively test a single connection dict. Updates the dict in place
    and caches the result. Returns the updated dict.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn["last_check"] = now
    raw_url = conn.get("_raw_url", "")
    conn_type = conn.get("type", "")

    # Skip testing connections that are not configured or disabled
    if conn["status"] in ("not_configured", "disabled") or not raw_url:
        _cache_set(conn["id"], conn)
        return conn

    # SMTP connections need a different check
    if conn_type == "smtp":
        ok, error = await _test_smtp(raw_url, conn.get("metadata", {}).get("port", 587))
        if ok:
            conn["status"] = "connected"
            conn["last_success"] = now
            conn["last_error"] = None
        else:
            conn["status"] = "error"
            conn["last_error"] = error
        _cache_set(conn["id"], conn)
        return conn

    # HTTP-based check
    headers = conn.get("_raw_headers") or {}
    ok, status_code, error = await _async_check_url(
        raw_url, headers=headers, timeout=_DEFAULT_TIMEOUT
    )

    if ok:
        conn["status"] = "connected"
        conn["last_success"] = now
        conn["last_error"] = None
        conn["metadata"]["status_code"] = status_code
    else:
        conn["status"] = "error"
        conn["last_error"] = error or "Unreachable"

    _cache_set(conn["id"], conn)
    return conn


async def _test_smtp(host: str, port: int = 587) -> tuple[bool, Optional[str]]:
    """Test SMTP connectivity via raw socket (no email sent)."""
    import socket
    loop = asyncio.get_running_loop()

    def _check() -> tuple[bool, Optional[str]]:
        try:
            sock = socket.create_connection((host, port), timeout=_DEFAULT_TIMEOUT)
            # Read the banner
            data = sock.recv(1024)
            sock.close()
            if data:
                return (True, None)
            return (False, "No SMTP banner received")
        except socket.timeout:
            return (False, "Connection timed out")
        except OSError as exc:
            return (False, str(exc))

    return await loop.run_in_executor(None, _check)


async def test_connection(connection_id: str) -> dict:
    """
    Actively test a specific connection by ID. Bypasses cache.
    Returns the updated connection dict.

    Raises ValueError if connection_id is not found.
    """
    # Gather fresh connections to find the target
    all_conns = await gather_all_connections()
    target = None
    for conn in all_conns:
        if conn["id"] == connection_id:
            target = conn
            break

    if target is None:
        raise ValueError(f"Unknown connection: {connection_id!r}")

    # Clear cache for this connection so we do a fresh test
    _cache.pop(connection_id, None)

    return await _test_single_connection(target)


async def test_all_connections() -> list[dict]:
    """
    Test all configured connections in parallel.
    Returns list of updated connection dicts with test results.
    """
    all_conns = await gather_all_connections()

    # Test all connections concurrently
    tasks = [_test_single_connection(conn) for conn in all_conns]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    tested: list[dict] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(
                "Connection test failed for %s: %s",
                all_conns[i]["id"], result,
            )
            conn = all_conns[i]
            conn["status"] = "error"
            conn["last_error"] = f"{type(result).__name__}: {result}"
            conn["last_check"] = datetime.now(timezone.utc).isoformat()
            tested.append(conn)
        else:
            tested.append(result)

    return tested


def sanitize_connection(conn: dict) -> dict:
    """
    Remove internal fields before sending to the client.
    Strips _raw_url and _raw_headers.
    """
    return {
        k: v for k, v in conn.items()
        if not k.startswith("_")
    }
