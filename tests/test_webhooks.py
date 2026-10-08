"""
Tests for orchestrator.api.webhooks
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.api.webhooks import _verify_nomus_signature


# ── HMAC signature verification ──────────────────────────────────────────────


class TestNomusSignatureVerification:
    def test_valid_signature(self):
        secret = "test-nomus-secret"
        body = b'{"event":"rules.updated","data":{}}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_nomus_signature(body, sig, secret) is True

    def test_invalid_signature(self):
        secret = "test-nomus-secret"
        body = b'{"event":"rules.updated","data":{}}'
        assert _verify_nomus_signature(body, "bad_signature", secret) is False

    def test_empty_secret(self):
        body = b'{"event":"rules.updated","data":{}}'
        assert _verify_nomus_signature(body, "any_sig", "") is False

    def test_empty_signature(self):
        body = b'{"event":"rules.updated","data":{}}'
        assert _verify_nomus_signature(body, "", "secret") is False

    def test_case_insensitive_comparison(self):
        secret = "test-secret"
        body = b'{"test": true}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_nomus_signature(body, sig.upper(), secret) is True


# ── Webhook endpoint integration tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_nomus_webhook_no_secret(client):
    """Returns 500 when neither nomus_webhook_secret nor nomus_api_key configured."""
    with patch("orchestrator.core.config.settings.nomus_webhook_secret", ""), \
         patch("orchestrator.core.config.settings.nomus_api_key", ""):
        resp = await client.post(
            "/api/v1/webhooks/nomus",
            json={"event": "rules.updated", "data": {}},
        )
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_nomus_webhook_uses_dedicated_secret(client):
    """Nomus webhook prefers nomus_webhook_secret over nomus_api_key."""
    dedicated_secret = "dedicated-webhook-secret"
    payload = {"event": "rules.updated", "data": {}}
    body = json.dumps(payload).encode()
    sig = hmac.new(dedicated_secret.encode(), body, hashlib.sha256).hexdigest()

    with patch("orchestrator.core.config.settings.nomus_webhook_secret", dedicated_secret), \
         patch("orchestrator.core.config.settings.nomus_api_key", "different-api-key"), \
         patch("orchestrator.core.nomus_client.sync_ruleset", new_callable=AsyncMock, return_value={"success": True}):
        resp = await client.post(
            "/api/v1/webhooks/nomus",
            content=body,
            headers={
                "X-Nomus-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_nomus_webhook_falls_back_to_api_key(client):
    """Nomus webhook falls back to nomus_api_key when webhook_secret is empty."""
    api_key = "nk_live_test_key"
    payload = {"event": "rules.error", "data": {"error": "test"}}
    body = json.dumps(payload).encode()
    sig = hmac.new(api_key.encode(), body, hashlib.sha256).hexdigest()

    with patch("orchestrator.core.config.settings.nomus_webhook_secret", ""), \
         patch("orchestrator.core.config.settings.nomus_api_key", api_key):
        resp = await client.post(
            "/api/v1/webhooks/nomus",
            content=body,
            headers={
                "X-Nomus-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_nomus_webhook_bad_json(client):
    """Returns 400 for invalid JSON payload."""
    secret = "test-secret"
    body = b"not valid json"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with patch("orchestrator.core.config.settings.nomus_webhook_secret", secret), \
         patch("orchestrator.core.config.settings.nomus_api_key", ""):
        resp = await client.post(
            "/api/v1/webhooks/nomus",
            content=body,
            headers={
                "X-Nomus-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 400
