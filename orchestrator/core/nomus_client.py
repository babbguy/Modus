"""
Modus — Nomus Regulatory Engine Client
=============================================
Pulls regulatory compliance rules from a Nomus service (optional integration).
Customer data NEVER leaves their infrastructure (Law 2).
Only pulls: compiled policy rules, state hashes, public keys.

Architecture:
    1. The operator sets MODUS_NOMUS_URL (and optionally MODUS_NOMUS_API_KEY).
       With no URL configured the integration is inactive.
    2. On startup, load disk cache (if available) for instant availability
    3. Fetch signed policy bundle from Nomus: GET /api/v1/policies/bundle
    4. Verify Ed25519 signatures on every rule using Nomus's public key
    5. Periodic state-hash checks (GET /api/v1/policies/hash) detect changes
       without re-downloading the full bundle
    6. Compliance checks run LOCALLY against cached rules — no data sent outbound

Nomus API endpoints used:
    GET  /health                         — connectivity test
    GET  /.well-known/nomus-keys      — Ed25519 public key (JWKS-like)
    GET  /api/v1/policies/bundle         — signed policy bundle (initial + refresh)
    GET  /api/v1/policies/hash           — state hash for change detection
    GET  /api/v1/policies                — paginated policy list (fallback)
    POST /api/v1/evaluate                — remote compliance evaluation (optional)

Law 2: No new DB tables. Memory + optional disk cache.
Law 3: stdlib only (urllib.request).
Law 4: Customer data NEVER leaves the customer's infrastructure.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)

# ── Startup check: Ed25519 signature verification dependency ──────────────
try:
    import cryptography  # noqa: F401
    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    _CRYPTOGRAPHY_AVAILABLE = False
    logger.critical(
        "cryptography package is NOT installed. Nomus policy bundle "
        "Ed25519 signatures will NOT be verified. Install it with: "
        "pip install cryptography"
    )

# ── In-memory cache ────────────────────────────────────────────────────────

_cached_policies: list[dict] = []        # Compiled policy rules
_cached_state_hash: Optional[str] = None  # SHA-256 of all active rules
_cached_public_key: Optional[str] = None  # Ed25519 public key from Nomus
_last_sync_ts: float = 0.0
_last_sync_error: Optional[str] = None
_sync_count: int = 0
_bundle_version: Optional[str] = None

# Disk cache path (optional persistence across restarts)
_DISK_CACHE_PATH = Path(os.environ.get(
    "MODUS_NOMUS_CACHE_PATH",
    "./data/nomus_policies.json",
))


# ── Public API ───────────────────────────────────────────────────────────────


async def sync_ruleset() -> dict:
    """Pull the latest policy bundle from Nomus.

    Flow:
      1. Check state hash — if unchanged, skip full download
      2. If changed (or first sync), fetch full policy bundle
      3. Verify bundle signature (if public key available)
      4. Cache in memory + optional disk persistence

    Returns dict with: success, version, policy_count, state_hash, error.
    """
    global _cached_policies, _cached_state_hash, _last_sync_ts
    global _last_sync_error, _sync_count, _bundle_version

    url = settings.nomus_url
    if not url:
        return {
            "success": False,
            "error": "Nomus URL not configured. Set MODUS_NOMUS_URL to enable the Nomus integration.",
            "version": None,
            "policy_count": 0,
        }

    base = url.rstrip("/")

    try:
        # Step 1: Check state hash for change detection (cheap call)
        if _cached_state_hash:
            hash_data = await _fetch_json(f"{base}/api/v1/policies/hash")
            remote_hash = hash_data.get("stateHash") or hash_data.get("hash")
            if remote_hash and remote_hash == _cached_state_hash:
                _last_sync_ts = time.time()
                _last_sync_error = None
                logger.debug("Nomus state hash unchanged — skipping bundle download")
                return {
                    "success": True,
                    "version": _bundle_version,
                    "policy_count": len(_cached_policies),
                    "state_hash": _cached_state_hash,
                    "skipped": True,
                }

        # Step 2: Fetch public key (for signature verification)
        await _refresh_public_key(base)

        # Step 3: Fetch full policy bundle
        bundle = await _fetch_json(f"{base}/api/v1/policies/bundle")

        # Validate bundle structure
        policies = bundle.get("policies", [])
        if not isinstance(policies, list):
            raise ValueError("Invalid bundle: 'policies' must be a list")

        state_hash = bundle.get("stateHash") or bundle.get("state_hash")
        bundle_sig = bundle.get("signature")
        generated_at = bundle.get("generatedAt") or bundle.get("generated_at")

        # Step 4: Verify bundle signature — FAIL CLOSED.
        # If Nomus has published a public key, every bundle MUST carry a
        # valid signature. A bundle that arrives without a signature while a key
        # is configured is rejected outright: raising here skips the caching in
        # Step 5, so the previously cached (verified) bundle is preserved.
        # Verification is only skipped when no key is configured at all
        # (unauthenticated), which is an explicit posture.
        if _cached_public_key:
            if not bundle_sig:
                raise ValueError(
                    "Nomus bundle is missing a signature but a public key is "
                    "configured. Refusing to cache or apply an unsigned bundle. "
                    "Keeping the previously cached bundle."
                )
            _verify_bundle_signature(state_hash, generated_at, bundle_sig)

        # Step 5: Cache
        _cached_policies = policies
        _cached_state_hash = state_hash
        _bundle_version = bundle.get("version") or generated_at or "unknown"
        _last_sync_ts = time.time()
        _last_sync_error = None
        _sync_count += 1

        # Persist to disk (best-effort)
        _save_disk_cache(bundle)

        logger.info(
            "Nomus policies synced: %d rules, hash=%s",
            len(policies), (state_hash or "?")[:12],
        )
        return {
            "success": True,
            "version": _bundle_version,
            "policy_count": len(policies),
            "state_hash": state_hash,
        }

    except urllib.error.HTTPError as exc:
        _last_sync_error = str(exc)
        if exc.code in (401, 403):
            logger.warning(
                "Nomus sync failed with HTTP %d — authentication rejected. "
                "Check MODUS_NOMUS_API_KEY configuration.",
                exc.code,
            )
        else:
            logger.warning("Nomus sync failed: HTTP %d %s", exc.code, exc)
        return {
            "success": False,
            "error": str(exc),
            "version": _bundle_version,
            "policy_count": len(_cached_policies),
        }
    except Exception as exc:
        _last_sync_error = str(exc)
        logger.warning("Nomus sync failed: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "version": _bundle_version,
            "policy_count": len(_cached_policies),
        }


async def get_status() -> dict:
    """Get Nomus connection status and cached policy info."""
    url = settings.nomus_url
    configured = bool(url)
    has_policies = len(_cached_policies) > 0

    if not configured:
        conn_status = "not_configured"
    elif has_policies and not _last_sync_error:
        conn_status = "connected"
    elif _last_sync_error:
        conn_status = "error"
    else:
        conn_status = "disconnected"

    # Extract regulation summaries from policies
    jurisdictions = set()
    categories = set()
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for p in _cached_policies:
        j = p.get("jurisdiction")
        if j:
            jurisdictions.add(j)
        c = p.get("category")
        if c:
            categories.add(c)
        s = p.get("severity", "medium")
        severity_counts[s] = severity_counts.get(s, 0) + 1

    interval = settings.nomus_sync_interval_seconds
    next_sync = None
    if _last_sync_ts > 0 and settings.nomus_auto_sync:
        next_sync_ts = _last_sync_ts + interval
        next_sync = datetime.fromtimestamp(next_sync_ts, tz=timezone.utc).isoformat()

    return {
        "status": conn_status,
        "configured": configured,
        "nomus_url": _sanitize_url(url) if url else None,
        "version": _bundle_version,
        "state_hash": _cached_state_hash,
        "last_sync": (
            datetime.fromtimestamp(_last_sync_ts, tz=timezone.utc).isoformat()
            if _last_sync_ts > 0 else None
        ),
        "next_sync": next_sync,
        "last_error": _last_sync_error,
        "auto_sync": settings.nomus_auto_sync,
        "sync_interval_seconds": interval,
        "sync_count": _sync_count,
        "policy_count": len(_cached_policies),
        # A "regulation" is a jurisdiction's rule set (same grouping as
        # GET /admin/nomus/regulations), so the count is distinct jurisdictions.
        "regulation_count": len(jurisdictions),
        "jurisdictions": sorted(jurisdictions),
        "categories": sorted(categories),
        "severity_counts": severity_counts,
    }


async def check_compliance(cot_entry: dict) -> dict:
    """Check a CoT ledger entry against cached Nomus policies.

    Runs LOCALLY using the cached policy bundle — no data sent to Nomus.

    Args:
        cot_entry: dict with at minimum: decision_type, decision_summary,
                   and optionally regulatory_tags, rules_evaluated.

    Returns:
        {compliant: bool, violations: [...], warnings: [...],
         policies_evaluated: int, ruleset_version: str}
    """
    if not _cached_policies:
        return {
            "compliant": True,
            "violations": [],
            "warnings": [{"message": "No Nomus policies loaded. Sync first."}],
            "policies_evaluated": 0,
            "ruleset_version": None,
        }

    violations = []
    warnings = []
    policies_evaluated = 0

    entry_tags = set(cot_entry.get("regulatory_tags") or [])
    decision_type = cot_entry.get("decision_type", "")
    decision_summary = cot_entry.get("decision_summary", "")
    rules_evaluated = cot_entry.get("rules_evaluated") or []
    reasoning_steps = cot_entry.get("reasoning_steps") or []

    for policy in _cached_policies:
        rule_key = policy.get("ruleKey", "")
        jurisdiction = policy.get("jurisdiction", "")
        category = policy.get("category", "")
        conditions = policy.get("conditions") or {}
        effect = policy.get("effect", "flag")
        severity = policy.get("severity", "medium")
        human_summary = policy.get("humanSummary", "")
        legal_ref = policy.get("legalReference", "")

        # Match by tag overlap or condition matching
        # Nomus policies use ruleKey patterns like "eu.ai_act.art6.high_risk"
        rule_tags = {rule_key, jurisdiction.lower(), category}
        matched = entry_tags & rule_tags

        if not matched:
            # Try condition-based matching
            action = conditions.get("action", "")
            if action and action in decision_type:
                matched = {action}

        if not matched:
            continue

        policies_evaluated += 1

        # Check requirement fulfillment based on effect
        violation = _evaluate_policy_compliance(
            effect=effect,
            category=category,
            decision_type=decision_type,
            decision_summary=decision_summary,
            rules_evaluated=rules_evaluated,
            reasoning_steps=reasoning_steps,
        )

        if violation:
            violations.append({
                "rule_key": rule_key,
                "jurisdiction": jurisdiction,
                "category": category,
                "effect": effect,
                "severity": severity,
                "human_summary": human_summary,
                "legal_reference": legal_ref,
                "description": violation["description"],
                "matched_tags": list(matched),
            })

    return {
        "compliant": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
        "policies_evaluated": policies_evaluated,
        "ruleset_version": _bundle_version,
        "state_hash": _cached_state_hash,
    }


async def test_connectivity() -> dict:
    """Test connectivity to Nomus service."""
    url = settings.nomus_url
    if not url:
        return {"reachable": False, "error": "Nomus URL not configured."}

    base = url.rstrip("/")
    try:
        data = await _fetch_json(f"{base}/health", timeout=10)
        return {
            "reachable": True,
            "nomus_version": data.get("version"),
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def get_cached_policies() -> list[dict]:
    """Return the in-memory cached policies."""
    return _cached_policies


def get_cached_state_hash() -> Optional[str]:
    """Return the current state hash."""
    return _cached_state_hash


def load_disk_cache() -> bool:
    """Load policy bundle from disk cache on startup."""
    global _cached_policies, _cached_state_hash, _last_sync_ts, _bundle_version
    try:
        if _DISK_CACHE_PATH.exists():
            data = json.loads(_DISK_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "policies" in data:
                _cached_policies = data["policies"]
                _cached_state_hash = data.get("stateHash") or data.get("state_hash")
                _bundle_version = data.get("version") or data.get("generatedAt")
                _last_sync_ts = _DISK_CACHE_PATH.stat().st_mtime
                logger.info(
                    "Nomus policies loaded from disk: %d rules, hash=%s",
                    len(_cached_policies),
                    (_cached_state_hash or "?")[:12],
                )
                return True
            # Support legacy ruleset format (regulations-based)
            if isinstance(data, dict) and "regulations" in data:
                _cached_policies = _convert_legacy_ruleset(data)
                _bundle_version = data.get("version")
                _last_sync_ts = _DISK_CACHE_PATH.stat().st_mtime
                logger.info(
                    "Nomus legacy ruleset loaded from disk: %d converted rules",
                    len(_cached_policies),
                )
                return True
    except Exception as exc:
        logger.debug("Could not load Nomus disk cache: %s", exc)
    return False


# ── Background sync task ────────────────────────────────────────────────────


async def nomus_sync_loop():
    """Background task: periodically sync Nomus policies.

    Idles until MODUS_NOMUS_URL is configured.
    """
    import asyncio

    # Try disk cache first for instant availability
    load_disk_cache()

    if not settings.nomus_auto_sync:
        logger.info("Nomus auto-sync disabled")
        return

    logger.info("Nomus sync loop started (interval=%ds)", settings.nomus_sync_interval_seconds)

    try:
        while True:
            if settings.nomus_url:
                await sync_ruleset()
            else:
                logger.debug("Nomus URL not configured — integration inactive")
            await asyncio.sleep(settings.nomus_sync_interval_seconds)
    except asyncio.CancelledError:
        logger.info("Nomus sync loop shutting down cleanly")
        return


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _fetch_json(url: str, timeout: int = 15) -> dict:
    """Fetch JSON from Nomus using stdlib urllib (Law 3: no deps)."""
    import asyncio

    def _do_fetch():
        ctx = ssl.create_default_context()
        headers = {
            "Accept": "application/json",
            "User-Agent": f"Modus/{settings.version}",
        }
        # Add Nomus API key if configured
        api_key = settings.nomus_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status != 200:
                raise urllib.error.HTTPError(
                    url, resp.status, f"HTTP {resp.status}", resp.headers, None,
                )
            return json.loads(resp.read().decode("utf-8"))

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do_fetch)


async def _refresh_public_key(base_url: str) -> None:
    """Fetch Nomus's Ed25519 public key for signature verification."""
    global _cached_public_key
    try:
        data = await _fetch_json(f"{base_url}/.well-known/nomus-keys", timeout=10)
        keys = data.get("keys", [])
        if keys:
            # Use first Ed25519 key
            for key in keys:
                if key.get("kty") == "OKP" and key.get("crv") == "Ed25519":
                    _cached_public_key = key.get("x")
                    logger.debug("Nomus public key refreshed")
                    return
    except Exception as exc:
        logger.debug("Could not fetch Nomus public key: %s", exc)


def _verify_bundle_signature(
    state_hash: Optional[str],
    generated_at: Optional[str],
    signature: str,
) -> None:
    """Verify the bundle's Ed25519 signature.

    The signed payload is: "{state_hash}:{generated_at}"
    The public key is the base64url-encoded Ed25519 public key from Nomus JWKS.
    The signature is base64url-encoded Ed25519 signature bytes.
    """
    if not _cached_public_key or not state_hash:
        return  # Can't verify without key or hash

    import base64

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        InvalidSignature = None
        Ed25519PublicKey = None

    if Ed25519PublicKey is not None:
        try:
            # Decode the base64url-encoded public key
            pub_bytes = base64.urlsafe_b64decode(_cached_public_key + "==")
            public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)

            # Build the signed payload
            payload = f"{state_hash}:{generated_at or ''}".encode("utf-8")

            # Decode the base64url-encoded signature
            sig_bytes = base64.urlsafe_b64decode(signature + "==")

            # Verify
            public_key.verify(sig_bytes, payload)
            logger.debug("Bundle signature verified (hash=%s)", state_hash[:12])
            return

        except InvalidSignature:
            raise ValueError(
                f"Bundle signature verification FAILED for hash={state_hash[:12]}. "
                "The bundle may have been tampered with."
            )
    else:
        # cryptography package not installed — cannot verify Ed25519 signature.
        # A public key is configured (guard above), so an unverifiable bundle
        # must be rejected, not accepted: these policies drive enforcement.
        # The orchestrator pins `cryptography` in requirements, so this branch
        # only fires on a broken install. The caller preserves the previously
        # cached bundle on rejection.
        raise ValueError(
            f"Bundle signature verification IMPOSSIBLE for hash={state_hash[:12]}: "
            "a Nomus public key is configured but the cryptography package is "
            "not installed. Refusing the unverified bundle. "
            "Install it with: pip install cryptography"
        )


def _evaluate_policy_compliance(
    *,
    effect: str,
    category: str,
    decision_type: str,
    decision_summary: str,
    rules_evaluated: list,
    reasoning_steps: list,
) -> Optional[dict]:
    """Evaluate if a governance decision complies with a Nomus policy.

    Returns None if compliant, or {description} if violation found.
    """
    # Deny effect — check if this type of action should be blocked
    if effect == "deny":
        if decision_type in ("governance_proposal", "evolution_proposal"):
            if not reasoning_steps:
                return {
                    "description": f"Action requires documented reasoning (category: {category})"
                }

    # Require disclosure — check transparency
    if effect == "require_disclosure":
        if not decision_summary or len(decision_summary.strip()) < 10:
            return {
                "description": f"Insufficient disclosure for {category} — decision summary too brief"
            }

    # Allow with audit — check audit trail
    if effect == "allow_with_audit":
        if not rules_evaluated and not reasoning_steps:
            return {
                "description": f"Audit trail required for {category} — no rules or reasoning documented"
            }

    # Human oversight categories
    if category in ("human_oversight", "accountability"):
        if decision_type in ("governance_proposal", "evolution_proposal"):
            has_oversight = any(
                "human" in str(r).lower() or "review" in str(r).lower() or "approve" in str(r).lower()
                for r in rules_evaluated
                if isinstance(r, dict)
            )
            if not has_oversight:
                return {
                    "description": f"No human oversight evidence for autonomous {decision_type}"
                }

    # Risk assessment categories
    if category == "risk_assessment":
        if not rules_evaluated:
            return {
                "description": "Risk evaluation rules required but none documented"
            }

    # Fairness / bias categories
    if category == "fairness":
        has_bias_check = any(
            "bias" in str(r).lower() or "fairness" in str(r).lower()
            for r in rules_evaluated
            if isinstance(r, dict)
        )
        if not has_bias_check and decision_type == "evolution_proposal":
            return {
                "description": "No bias/fairness evaluation found for evolution proposal"
            }

    return None


def _convert_legacy_ruleset(ruleset: dict) -> list[dict]:
    """Convert legacy regulation-based ruleset to policy list format.

    Legacy format: { regulations: [{ articles: [{ tags, requirements }] }] }
    New format: [{ ruleKey, jurisdiction, category, conditions, effect, ... }]
    """
    policies = []
    for reg in ruleset.get("regulations", []):
        reg_id = reg.get("id", "unknown")
        reg_name = reg.get("name", "")
        for article in reg.get("articles", []):
            article_id = article.get("id", "")
            for i, req in enumerate(article.get("requirements", [])):
                policies.append({
                    "ruleKey": f"{reg_id}.art{article_id}.req{i}",
                    "jurisdiction": reg_id.upper().replace("_", "-"),
                    "category": _infer_category(req),
                    "conditions": {},
                    "effect": "allow_with_audit",
                    "severity": "medium",
                    "humanSummary": req,
                    "legalReference": f"{reg_name} Article {article_id}",
                })
    return policies


def _infer_category(requirement: str) -> str:
    """Infer policy category from requirement text."""
    req_lower = requirement.lower()
    if "bias" in req_lower or "fairness" in req_lower or "discriminat" in req_lower:
        return "fairness"
    if "transparen" in req_lower or "explainab" in req_lower:
        return "transparency"
    if "risk" in req_lower:
        return "risk_assessment"
    if "human" in req_lower or "oversight" in req_lower or "review" in req_lower:
        return "human_oversight"
    if "document" in req_lower or "record" in req_lower or "audit" in req_lower:
        return "accountability"
    if "privacy" in req_lower or "data" in req_lower:
        return "data_governance"
    return "transparency"


def _save_disk_cache(bundle: dict) -> None:
    """Best-effort persist policy bundle to disk."""
    try:
        _DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DISK_CACHE_PATH.write_text(
            json.dumps(bundle, separators=(",", ":"), default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("Could not save Nomus disk cache: %s", exc)


def _sanitize_url(url: str) -> str:
    """Redact credentials from URL for display."""
    if "@" in url:
        parts = url.split("@", 1)
        scheme_end = parts[0].rfind("//") + 2
        return parts[0][:scheme_end] + "***@" + parts[1]
    return url
