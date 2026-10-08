"""
Modus — TLS 1.3 Enforcement Utility
==========================================
Provides a shared SSL context that enforces TLS 1.3 minimum for all
outbound connections. Protects against protocol downgrade attacks and
ensures forward secrecy via TLS 1.3 mandatory cipher suites.

Usage:
    from orchestrator.core.tls import get_tls_context, get_httpx_client

    # For urllib
    urllib.request.urlopen(req, context=get_tls_context())

    # For httpx
    async with get_httpx_client(timeout=30) as client: ...

    # For SMTP
    smtp.starttls(context=get_tls_context())
"""
from __future__ import annotations

import ssl
from functools import lru_cache

import httpx


@lru_cache(maxsize=1)
def get_tls_context() -> ssl.SSLContext:
    """Return a TLS 1.3+ SSL context for outbound connections."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_default_certs()
    return ctx


def get_httpx_client(
    timeout: float = 30.0,
    *,
    enforce_tls: bool = True,
) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient with TLS 1.3 enforcement.

    Set enforce_tls=False for localhost/plaintext connections (e.g., Ollama).
    """
    kwargs: dict = {"timeout": timeout}
    if enforce_tls:
        kwargs["verify"] = get_tls_context()
    return httpx.AsyncClient(**kwargs)
