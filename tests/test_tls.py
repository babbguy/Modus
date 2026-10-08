"""
Tests for orchestrator.core.tls — TLS context creation and httpx client.
"""
from __future__ import annotations

import ssl

import httpx

from orchestrator.core.tls import get_httpx_client, get_tls_context


class TestGetTlsContext:
    def test_returns_ssl_context(self):
        ctx = get_tls_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_enforces_tls_13(self):
        ctx = get_tls_context()
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3

    def test_check_hostname(self):
        ctx = get_tls_context()
        assert ctx.check_hostname is True

    def test_verify_mode(self):
        ctx = get_tls_context()
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_cached(self):
        ctx1 = get_tls_context()
        ctx2 = get_tls_context()
        assert ctx1 is ctx2


class TestGetHttpxClient:
    def test_returns_async_client(self):
        client = get_httpx_client()
        assert isinstance(client, httpx.AsyncClient)

    def test_custom_timeout(self):
        client = get_httpx_client(timeout=5.0)
        assert isinstance(client, httpx.AsyncClient)

    def test_no_tls_enforcement(self):
        client = get_httpx_client(enforce_tls=False)
        assert isinstance(client, httpx.AsyncClient)
