"""
Tests for orchestrator.core.nomus_client
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from orchestrator.core import nomus_client as gc


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_bundle(policies=None, state_hash="abc123", version="v1"):
    return {
        "policies": policies or [{"ruleKey": "test.rule", "jurisdiction": "US", "category": "transparency"}],
        "stateHash": state_hash,
        "version": version,
        "generatedAt": "2026-01-01T00:00:00Z",
        "signature": "fakesig",
    }


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset module-level cache between tests."""
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


# ── sync_ruleset tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_ruleset_no_url():
    """sync_ruleset returns error when Nomus URL not configured."""
    with patch.object(gc.settings, "nomus_url", ""):
        result = await gc.sync_ruleset()
    assert result["success"] is False
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_sync_ruleset_success():
    """sync_ruleset fetches and caches policies."""
    bundle = _make_bundle()

    async def mock_fetch(url, timeout=15):
        if "well-known" in url:
            return {"keys": []}
        return bundle

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc, "_fetch_json", side_effect=mock_fetch), \
         patch.object(gc, "_save_disk_cache"):
        result = await gc.sync_ruleset()

    assert result["success"] is True
    assert result["policy_count"] == 1
    assert result["state_hash"] == "abc123"
    assert gc._cached_policies == bundle["policies"]
    assert gc._sync_count == 1


@pytest.mark.asyncio
async def test_sync_ruleset_skips_when_hash_unchanged():
    """sync_ruleset skips download when state hash matches."""
    gc._cached_state_hash = "same_hash"
    gc._cached_policies = [{"ruleKey": "existing"}]
    gc._bundle_version = "v1"

    async def mock_fetch(url, timeout=15):
        return {"stateHash": "same_hash"}

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc, "_fetch_json", side_effect=mock_fetch):
        result = await gc.sync_ruleset()

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["policy_count"] == 1


@pytest.mark.asyncio
async def test_sync_ruleset_error_handling():
    """sync_ruleset handles network errors gracefully."""
    async def mock_fetch(url, timeout=15):
        raise ConnectionError("Network unreachable")

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc, "_fetch_json", side_effect=mock_fetch):
        result = await gc.sync_ruleset()

    assert result["success"] is False
    assert "Network unreachable" in result["error"]


@pytest.mark.asyncio
async def test_sync_ruleset_rejects_unsigned_bundle_when_key_configured():
    """Fail closed: when a public key is configured, a bundle lacking a
    signature must be rejected and the previously cached bundle preserved."""
    gc._cached_public_key = "preconfigured_key"
    gc._cached_policies = [{"ruleKey": "previously.cached"}]
    gc._cached_state_hash = "old_hash"
    gc._bundle_version = "v-old"

    unsigned = {
        "policies": [{"ruleKey": "new.unsigned"}],
        "stateHash": "new_hash",
        "version": "v-new",
        "generatedAt": "2026-01-01T00:00:00Z",
        # no "signature" key
    }

    async def mock_fetch(url, timeout=15):
        if "well-known" in url:
            # Key already cached; return no fresh keys.
            return {"keys": []}
        if url.endswith("/hash"):
            return {"stateHash": "new_hash"}
        return unsigned

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc, "_fetch_json", side_effect=mock_fetch), \
         patch.object(gc, "_save_disk_cache"):
        result = await gc.sync_ruleset()

    assert result["success"] is False
    assert "signature" in result["error"].lower()
    # Previous cache is untouched (graceful degradation).
    assert gc._cached_policies == [{"ruleKey": "previously.cached"}]
    assert gc._cached_state_hash == "old_hash"


@pytest.mark.asyncio
async def test_sync_ruleset_allows_unsigned_bundle_when_no_key():
    """When no public key is configured at all, verification is skipped and
    the (unsigned) bundle is accepted — the explicit free-tier posture."""
    unsigned = {
        "policies": [{"ruleKey": "free.tier"}],
        "stateHash": "h1",
        "version": "v1",
        "generatedAt": "2026-01-01T00:00:00Z",
    }

    async def mock_fetch(url, timeout=15):
        if "well-known" in url:
            return {"keys": []}
        return unsigned

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc, "_fetch_json", side_effect=mock_fetch), \
         patch.object(gc, "_save_disk_cache"):
        result = await gc.sync_ruleset()

    assert result["success"] is True
    assert gc._cached_policies == unsigned["policies"]


# ── Bundle signature verification tests ──────────────────────────────────────


def test_verify_bundle_signature_skips_without_key():
    """Verification is skipped when no public key is cached."""
    gc._cached_public_key = None
    # Should not raise
    gc._verify_bundle_signature("hash123", "2026-01-01T00:00:00Z", "sig")


def test_verify_bundle_signature_skips_without_hash():
    """Verification is skipped when no state hash provided."""
    gc._cached_public_key = "some_key"
    gc._verify_bundle_signature(None, "2026-01-01T00:00:00Z", "sig")


def test_verify_bundle_signature_rejects_without_cryptography():
    """With a public key configured but cryptography unavailable, the bundle
    is rejected (fail closed) rather than skipped: these policies drive
    enforcement, and an unverifiable signature must not be trusted."""
    gc._cached_public_key = "test_public_key"
    state_hash = "abc123"
    generated_at = "2026-01-01T00:00:00Z"

    # Patch out cryptography import to simulate a broken install
    import builtins
    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if "cryptography" in name:
            raise ImportError("mocked")
        return original_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=mock_import):
        with pytest.raises(ValueError, match="IMPOSSIBLE"):
            gc._verify_bundle_signature(state_hash, generated_at, "any-signature")


# ── Disk cache tests ────────────────────────────────────────────────────────


def test_load_disk_cache_success(tmp_path):
    """load_disk_cache loads policies from a valid cache file."""
    cache_file = tmp_path / "nomus_policies.json"
    bundle = _make_bundle()
    cache_file.write_text(json.dumps(bundle))

    with patch.object(gc, "_DISK_CACHE_PATH", cache_file):
        result = gc.load_disk_cache()

    assert result is True
    assert len(gc._cached_policies) == 1
    assert gc._cached_state_hash == "abc123"


def test_load_disk_cache_missing_file(tmp_path):
    """load_disk_cache returns False when file doesn't exist."""
    with patch.object(gc, "_DISK_CACHE_PATH", tmp_path / "nonexistent.json"):
        result = gc.load_disk_cache()
    assert result is False


def test_load_disk_cache_legacy_format(tmp_path):
    """load_disk_cache handles legacy regulation-based format."""
    cache_file = tmp_path / "nomus_policies.json"
    legacy = {
        "regulations": [{
            "id": "eu_ai_act",
            "name": "EU AI Act",
            "articles": [{
                "id": "6",
                "requirements": ["Must assess risk before deployment"],
            }],
        }],
        "version": "legacy-v1",
    }
    cache_file.write_text(json.dumps(legacy))

    with patch.object(gc, "_DISK_CACHE_PATH", cache_file):
        result = gc.load_disk_cache()

    assert result is True
    assert len(gc._cached_policies) == 1
    assert gc._cached_policies[0]["ruleKey"] == "eu_ai_act.art6.req0"


# ── Compliance check tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_compliance_no_policies():
    """Returns compliant with warning when no policies loaded."""
    result = await gc.check_compliance({"decision_type": "test"})
    assert result["compliant"] is True
    assert len(result["warnings"]) == 1
    assert result["policies_evaluated"] == 0


@pytest.mark.asyncio
async def test_check_compliance_with_matching_policy():
    """Returns violation when policy matches and conditions aren't met."""
    gc._cached_policies = [{
        "ruleKey": "test.rule",
        "jurisdiction": "US",
        "category": "risk_assessment",
        "conditions": {},
        "effect": "allow_with_audit",
        "severity": "high",
        "humanSummary": "Risk assessment required",
        "legalReference": "Test Law Section 1",
    }]
    gc._bundle_version = "v1"

    entry = {
        "decision_type": "governance_proposal",
        "decision_summary": "A proposal",
        "regulatory_tags": ["test.rule"],
        "rules_evaluated": [],
        "reasoning_steps": [],
    }

    result = await gc.check_compliance(entry)
    assert result["policies_evaluated"] == 1


@pytest.mark.asyncio
async def test_check_compliance_deny_effect():
    """Deny effect with no reasoning triggers violation."""
    gc._cached_policies = [{
        "ruleKey": "deny.rule",
        "jurisdiction": "EU",
        "category": "accountability",
        "conditions": {},
        "effect": "deny",
        "severity": "critical",
        "humanSummary": "Must document reasoning",
        "legalReference": "Art 14",
    }]
    gc._bundle_version = "v1"

    entry = {
        "decision_type": "governance_proposal",
        "decision_summary": "A proposal without reasoning",
        "regulatory_tags": ["deny.rule"],
        "rules_evaluated": [],
        "reasoning_steps": [],
    }

    result = await gc.check_compliance(entry)
    assert result["compliant"] is False
    assert len(result["violations"]) == 1
    assert result["violations"][0]["severity"] == "critical"


# ── get_status tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_status_not_configured():
    """Status shows not_configured when URL is empty."""
    with patch.object(gc.settings, "nomus_url", ""):
        status = await gc.get_status()
    assert status["status"] == "not_configured"
    assert status["configured"] is False


@pytest.mark.asyncio
async def test_get_status_connected():
    """Status shows connected when policies are loaded without errors."""
    gc._cached_policies = [{"ruleKey": "test"}]
    gc._last_sync_error = None
    gc._last_sync_ts = time.time()

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc.settings, "nomus_auto_sync", True), \
         patch.object(gc.settings, "nomus_sync_interval_seconds", 3600):
        status = await gc.get_status()

    assert status["status"] == "connected"
    assert status["policy_count"] == 1


# ── test_connectivity tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connectivity_no_url():
    """Returns not reachable when URL is empty."""
    with patch.object(gc.settings, "nomus_url", ""):
        result = await gc.test_connectivity()
    assert result["reachable"] is False


@pytest.mark.asyncio
async def test_connectivity_success():
    """Returns reachable when Nomus responds."""
    async def mock_fetch(url, timeout=10):
        return {"status": "ok", "version": "1.0.0"}

    with patch.object(gc.settings, "nomus_url", "https://nomus.example.com"), \
         patch.object(gc, "_fetch_json", side_effect=mock_fetch):
        result = await gc.test_connectivity()

    assert result["reachable"] is True
    assert result["nomus_version"] == "1.0.0"
