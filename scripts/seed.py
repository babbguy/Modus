#!/usr/bin/env python3
"""
Modus — Development Seed Script
========================================
Creates the minimum required data for a working dev environment:
  1. Default dev team
  2. Registration token for that team
  3. Sample governance policies (budget cap, model denylist, rate limit)
  4. Writes .env.agents with the token so dummy apps can self-register

Idempotent — safe to run multiple times. Existing data is not overwritten.

Usage:
    python3 scripts/seed.py

Requires a running orchestrator and:
    MODUS_ORCHESTRATOR_URL — orchestrator base URL (default http://localhost:8080)
    MODUS_MASTER_API_KEY   — the orchestrator's master key

The master key is sent as X-Modus-APIKey on every request, which the
orchestrator accepts in both stub and jwt auth modes, so this works against a
jwt-mode stack too (no admin JWT needed).

`make seed` sets both from .env for the Docker Compose stack.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_URL = os.getenv("MODUS_ORCHESTRATOR_URL", "http://localhost:8080")
MASTER_KEY = os.getenv(
    "MODUS_MASTER_API_KEY",
    "mds_master_devkey_do_not_use_in_production_00000",
)
TEAM_SLUG = os.getenv("SEED_TEAM_SLUG", "dev-team")
TEAM_NAME = os.getenv("SEED_TEAM_NAME", "Dev Team")

REPO_ROOT = Path(__file__).parent.parent
ENV_AGENTS_PATH = REPO_ROOT / ".env.agents"


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: dict | None = None, token: str = MASTER_KEY) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Modus-APIKey": token,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        return {"_error": e.code, "_body": body_text}


def _ok(result: dict) -> bool:
    return "_error" not in result


# ── Seed steps ─────────────────────────────────────────────────────────────────

def seed_team() -> tuple[str, bool]:
    """Create dev team if it doesn't exist. Returns (team_id, created)."""
    # Check if team exists
    result = _req("GET", "/api/v1/teams")
    if _ok(result):
        for team in result:
            if team.get("slug") == TEAM_SLUG:
                print(f"  ✓ Team '{TEAM_SLUG}' already exists (id={team['id'][:8]}...)")
                return team["id"], False

    # Create it
    result = _req("POST", "/api/v1/teams", {"slug": TEAM_SLUG, "name": TEAM_NAME})
    if not _ok(result):
        print(f"  ✗ Failed to create team: {result.get('_body')}")
        sys.exit(1)

    print(f"  ✓ Created team '{TEAM_SLUG}' (id={result['id'][:8]}...)")
    return result["id"], True


def seed_registration_token(team_id: str) -> str | None:
    """Generate a registration token for the team. Returns token string."""
    result = _req("POST", f"/api/v1/teams/{team_id}/registration-token")
    if not _ok(result):
        err = result.get("_body", "")
        # Token may already exist — that's fine, we just can't retrieve it
        if "already" in err.lower() or "409" in str(result.get("_error", "")):
            print("  ✓ Team already has a registration token (cannot retrieve — check .env.agents)")
            return None
        print(f"  ✗ Failed to generate token: {err}")
        return None

    token = result["registration_token"]
    print(f"  ✓ Generated team registration token ({result['token_prefix']}...)")
    return token


def seed_policies(team_id: str) -> None:
    """Create sample governance policies useful for stress testing."""

    policies = [
        {
            "name": "Dev Daily Budget Cap — $10",
            "description": "Hard stop at $10/day per app in dev. Keeps runaway stress tests cheap.",
            "policy_type": "budget_cap",
            "scope": "team",
            "team_id": team_id,
            "effect": "deny",
            "priority": 100,
            "conditions": {},
            "config": {"cap_usd": "10.00", "period": "daily"},
            "action": {
                "message": "Daily budget cap of $10 reached. Resets at midnight UTC.",
            },
        },
        {
            "name": "Dev Hourly Rate Limit — 200 calls",
            "description": "Throttle after 200 AI calls/hour. Identifies runaway loops in stress tests.",
            "policy_type": "rate_limit",
            "scope": "team",
            "team_id": team_id,
            "effect": "throttle",
            "priority": 200,
            "conditions": {},
            "config": {"max_calls": 200, "window_seconds": 3600},
            "action": {
                "message": "Rate limit reached. Retry in 60 seconds.",
                "retry_after_seconds": 60,
            },
        },
        {
            "name": "Block GPT-4 in Dev",
            "description": "Prevent expensive GPT-4 calls in dev environments.",
            "policy_type": "model_denylist",
            "scope": "team",
            "team_id": team_id,
            "effect": "deny",
            "priority": 50,
            "conditions": {"environments": ["dev", "development"]},
            "config": {"models": ["gpt-4", "gpt-4-turbo", "gpt-4o"]},
            "action": {
                "message": "GPT-4 is not permitted in dev. Use gpt-3.5-turbo or claude-haiku.",
                "suggested_model": "claude-haiku-4-5-20251001",
            },
        },
        {
            "name": "Warn on claude-opus in Dev",
            "description": "Warn (but don't block) when Opus is used in dev. Useful for spotting over-engineering.",
            "policy_type": "model_denylist",
            "scope": "team",
            "team_id": team_id,
            "effect": "warn",
            "priority": 300,
            "conditions": {"environments": ["dev", "development"]},
            "config": {"models": ["claude-opus-4-6"]},
            "action": {
                "message": "claude-opus-4-6 in dev? Consider claude-haiku for cost efficiency.",
                "suggested_model": "claude-haiku-4-5-20251001",
            },
        },
    ]

    existing = _req("GET", "/api/v1/policies?active_only=false")
    existing_names = (
        {p.get("name") for p in existing if p.get("team_id") == team_id}
        if _ok(existing) and isinstance(existing, list) else set()
    )

    created = 0
    for policy in policies:
        if policy["name"] in existing_names:
            continue  # already seeded
        result = _req("POST", "/api/v1/policies", policy)
        if _ok(result):
            created += 1
        elif result.get("_error") == 409:
            pass  # Already exists
        else:
            print(f"  ⚠ Policy '{policy['name']}' failed: {result.get('_body', '')[:80]}")

    print(f"  ✓ {created}/{len(policies)} sample policies created")


def write_env_agents(token: str) -> None:
    """Write .env.agents so dummy app containers can self-register."""
    content = f"""# Auto-generated by scripts/seed.py — DO NOT COMMIT
# Used by dummy app containers for stress testing.
# Regenerate by deleting this file and running: make seed

MODUS_URL=http://modus:8080
MODUS_TEAM_TOKEN={token}
MODUS_ENVIRONMENT=dev
MODUS_FAIL_OPEN=true
"""
    ENV_AGENTS_PATH.write_text(content, encoding="utf-8")
    print(f"  ✓ Wrote {ENV_AGENTS_PATH.relative_to(REPO_ROOT)}")


def wait_for_orchestrator(max_attempts: int = 30) -> None:
    """Poll /health until the orchestrator is ready."""
    import time
    print("  Waiting for orchestrator...")
    for i in range(max_attempts):
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2) as resp:
                if resp.status == 200:
                    print("  ✓ Orchestrator ready")
                    return
        except Exception:
            pass
        time.sleep(2)
        if (i + 1) % 5 == 0:
            print(f"  ... still waiting ({(i+1)*2}s)")
    print("  ✗ Orchestrator did not become ready in time")
    sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # The status output uses symbols a legacy Windows console code page
    # cannot encode; replace them rather than crash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print("\n🌱 Modus Dev Seed")
    print("=" * 40)

    print("\n[1/4] Checking orchestrator health")
    wait_for_orchestrator()

    print("\n[2/4] Team")
    team_id, _ = seed_team()

    print("\n[3/4] Registration token")
    token = seed_registration_token(team_id)

    print("\n[4/4] Sample policies")
    seed_policies(team_id)

    if token:
        print("\n[+] Writing agent credentials")
        write_env_agents(token)

    print("\n✅ Seed complete")
    print(f"\n   Dashboard:     {BASE_URL}")
    print(f"   API docs:      {BASE_URL}/docs  (development mode only)")
    if token:
        print(f"\n   Team token:    {token[:24]}...")
        print("   Share with devs or set in their MODUS_TEAM_TOKEN")
    print()


if __name__ == "__main__":
    main()
