"""
Tests for orchestrator.api.nomus_integration — Nomus integration endpoints
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from orchestrator.core import nomus_client as gc


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_nomus_cache():
    """Reset nomus_client module-level cache between tests."""
    gc._cached_policies = []
    gc._cached_state_hash = None
    gc._cached_public_key = None
    gc._last_sync_ts = 0.0
    gc._last_sync_error = None
    gc._sync_count = 0
    gc._bundle_version = None
    yield
    gc._cached_policies = []
    gc._cached_state_hash = None
    gc._cached_public_key = None
    gc._last_sync_ts = 0.0
    gc._last_sync_error = None
    gc._sync_count = 0
    gc._bundle_version = None


# ── Nomus status endpoint ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nomus_integration_status_not_configured(client):
    """GET /integrations/nomus/status returns not-configured when URL empty."""
    with patch.object(gc.settings, "nomus_url", ""):
        resp = await client.get("/api/v1/integrations/nomus/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is False
    assert data["connected"] is False
    assert data["regulation_count"] == 0
    assert data["policy_count"] == 0


@pytest.mark.asyncio
async def test_nomus_integration_status_connected(client):
    """GET /integrations/nomus/status shows connected when policies cached."""
    gc._cached_policies = [
        {
            "ruleKey": "eu_ai_act.art6",
            "jurisdiction": "EU",
            "category": "risk_assessment",
            "severity": "high",
            "effect": "deny",
            "humanSummary": "High-risk AI must undergo conformity assessment",
            "legalReference": "EU AI Act Article 6",
        },
        {
            "ruleKey": "us_exec_order.sec3",
            "jurisdiction": "US",
            "category": "transparency",
            "severity": "medium",
            "effect": "flag",
            "humanSummary": "AI systems must disclose capabilities",
            "legalReference": "EO 14110 Section 3",
        },
    ]
    gc._cached_state_hash = "abc123"
    gc._last_sync_ts = time.time()
    gc._last_sync_error = None
    gc._sync_count = 2
    gc._bundle_version = "v2.1"

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc.settings, "nomus_auto_sync", True), \
         patch.object(gc.settings, "nomus_sync_interval_seconds", 3600):
        resp = await client.get("/api/v1/integrations/nomus/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["connected"] is True
    assert data["policy_count"] == 2
    assert data["regulation_count"] == 2  # EU + US
    assert data["compliance_score"] is not None
    assert data["state_hash"] == "abc123"
    assert data["version"] == "v2.1"
    assert "EU" in data["jurisdictions"]
    assert "US" in data["jurisdictions"]


@pytest.mark.asyncio
async def test_nomus_integration_status_with_error(client):
    """Status shows disconnected when last sync had an error."""
    gc._last_sync_error = "Connection refused"
    gc._cached_policies = []

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc.settings, "nomus_auto_sync", True), \
         patch.object(gc.settings, "nomus_sync_interval_seconds", 3600):
        resp = await client.get("/api/v1/integrations/nomus/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["connected"] is False


# ── Nomus policies endpoint ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nomus_integration_policies_empty(client):
    """GET /integrations/nomus/policies returns empty when no policies cached."""
    resp = await client.get("/api/v1/integrations/nomus/policies")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["policies"] == []
    assert "issued_at" in data


@pytest.mark.asyncio
async def test_nomus_integration_policies_with_data(client):
    """GET /integrations/nomus/policies returns cached policy summaries."""
    gc._cached_policies = [
        {
            "ruleKey": "eu_ai_act.art6",
            "jurisdiction": "EU",
            "category": "risk_assessment",
            "severity": "high",
            "effect": "deny",
            "humanSummary": "High-risk AI must undergo conformity assessment",
            "legalReference": "EU AI Act Article 6",
        },
    ]
    gc._cached_state_hash = "hash456"
    gc._bundle_version = "v3.0"

    resp = await client.get("/api/v1/integrations/nomus/policies")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["state_hash"] == "hash456"
    assert data["version"] == "v3.0"

    policy = data["policies"][0]
    assert policy["rule_key"] == "eu_ai_act.art6"
    assert policy["jurisdiction"] == "EU"
    assert policy["category"] == "risk_assessment"
    assert policy["severity"] == "high"
    assert policy["effect"] == "deny"
    assert policy["human_summary"] == "High-risk AI must undergo conformity assessment"
    assert policy["legal_reference"] == "EU AI Act Article 6"
