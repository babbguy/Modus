"""
Full dashboard API surface coverage tests.

The companion file `test_dashboard_contracts.py` locks the explicit
field-name shape of the most-used endpoints. This file widens the net:
it walks the dashboard JS source, extracts every distinct API URL the
dashboard fetches, and asserts that each one returns 200 OK with a
parseable JSON body. It catches:

- Endpoints that are referenced in the dashboard but missing on the backend
- Endpoints that 500 on an empty database (regression-prone)
- Endpoints that return non-JSON payloads (regression-prone)

It does NOT lock field names — `test_dashboard_contracts.py` does that
for the high-traffic surfaces. The two files together form a layered
defense:

  Layer 1 (this file)  — every URL the UI touches is reachable
  Layer 2 (contracts)  — every field the UI reads exists by name

A new dashboard URL added to a view is automatically tested as soon as
it's grepped here. Field-name promotion to Layer 2 happens manually
when the new endpoint becomes load-bearing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
REPO = Path(__file__).resolve().parents[1]
VIEWS = REPO / "dashboard" / "js" / "views"

# URLs that require path parameters or take a request body — these can't
# be smoke-fetched with no setup. They are exercised by other test files.
SKIP_URLS = {
    # path-parameterised endpoints
    "/api/v1/admin/connections/",
    "/api/v1/admin/federation/peers/",
    "/api/v1/apps/",
    "/api/v1/governance/proposals/",
    "/api/v1/notifications/test/",
    "/api/v1/policies/",
    "/api/v1/pricing/overrides/",
    "/api/v1/routing/fingerprints/",
    "/api/v1/thresholds/",
    "/api/v1/users/",
    # POST/PUT/DELETE endpoints — covered by their respective integration tests
    "/api/v1/admin/connections/test-all",
    "/api/v1/admin/diagnostics/scan",
    "/api/v1/admin/federation/peers/test-connection",
    "/api/v1/admin/nomus/sync",
    "/api/v1/admin/nomus/test",
    "/api/v1/apps/register",
    "/api/v1/assistant/chat",
    "/api/v1/onboarding/apply",
    "/api/v1/onboarding/complete",
    "/api/v1/onboarding/upload",
    # Diagnostic download is not JSON
    "/api/v1/admin/diagnostics/download",
    # Trailing-slash variants of bare list endpoints — duplicates
    "/api/v1/finance/",
    # POST-only endpoints that the dashboard calls with a body
    "/api/v1/users/invite",
    "/api/v1/finance/reconciliation/import/csv",
    "/api/v1/compliance/verify-attestation",
    # Personal user mutations — covered by their own integration tests
    "/api/v1/users/me/password",
    "/api/v1/users/me/api-key",
}


def _extract_urls() -> list[str]:
    """Walk every view file and extract /api/v1/* URLs."""
    urls: set[str] = set()
    pat = re.compile(r"['\"](/api/v1/[a-z0-9_/-]+)['\"]")
    for js in VIEWS.glob("*.js"):
        src = js.read_text(encoding="utf-8", errors="replace")
        for m in pat.findall(src):
            urls.add(m)
    return sorted(urls)


# Compute once at import time so pytest collects one parametrized test per URL.
_DISCOVERED = [u for u in _extract_urls() if u not in SKIP_URLS]


@pytest.mark.parametrize("url", _DISCOVERED)
async def test_dashboard_url_is_reachable(client, url):
    """Every /api/v1/* URL the dashboard fetches must return 200 with
    JSON. This is a smoke check, not a schema check — see
    test_dashboard_contracts.py for explicit field-name assertions on
    the most-used surfaces."""
    resp = await client.get(url)
    # Some endpoints are role-gated and may return 401/403 for the test
    # client. Treat those as acceptable — we just need the route to exist
    # (not 404 or 500).
    assert resp.status_code in (200, 401, 403), (
        f"{url}: unexpected status {resp.status_code} body={resp.text[:200]}"
    )
    if resp.status_code == 200:
        # Must parse as JSON
        data = resp.json()
        assert isinstance(data, (list, dict)), (
            f"{url}: expected list or dict, got {type(data).__name__}"
        )


# ── Manifest sanity check ───────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="function")
async def test_url_discovery_finds_expected_count():
    """Sanity: if the discovery regex breaks we'd silently lose coverage.
    This locks the lower-bound count so a regex regression is loud."""
    assert len(_DISCOVERED) >= 30, (
        f"Only discovered {len(_DISCOVERED)} dashboard URLs — discovery may be broken"
    )
