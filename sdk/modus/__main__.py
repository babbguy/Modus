"""
python -m modus diagnose
==================================
Self-diagnostic tool for Modus SDK.

Run this in any environment to verify:
  - Environment variables are set correctly
  - Network connectivity to the orchestrator
  - AI SDKs are installed and will be tracked
  - Registration succeeds with the provided team token

Usage:
    python -m modus diagnose
    python -m modus diagnose --url https://modus.corp.com
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib import request as urllib_request
from urllib.error import URLError, HTTPError


# ── Colours (graceful fallback for no-tty) ────────────────────────────────────

def _c(code: str, text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

def ok(msg: str)   -> str: return _c("32", f"  ✓  {msg}")
def warn(msg: str) -> str: return _c("33", f"  ⚠  {msg}")
def fail(msg: str) -> str: return _c("31", f"  ✗  {msg}")
def info(msg: str) -> str: return _c("36", f"  →  {msg}")
def head(msg: str) -> str: return _c("1",  f"\n{msg}")


# ── Provider detection ────────────────────────────────────────────────────────

PROVIDERS = [
    ("anthropic",     "anthropic",               "claude-*"),
    ("openai",        "openai",                  "gpt-4o, o3, grok-* (via openai SDK)"),
    ("boto3/bedrock", "boto3",                   "all Bedrock models"),
    ("google-genai",  "google.genai",            "gemini-2.0-flash, gemini-1.5-pro"),
    ("google-legacy", "google.generativeai",     "gemini (legacy SDK)"),
    ("groq",          "groq",                    "llama-3, mixtral, deepseek"),
    ("mistral",       "mistralai",               "mistral-large, codestral"),
    ("cohere",        "cohere",                  "command-r-plus, command-r"),
]


def _check_providers() -> list[str]:
    found = []
    for label, module, models in PROVIDERS:
        try:
            parts = module.split(".")
            m = __import__(parts[0])
            for part in parts[1:]:
                m = getattr(m, part)
            ver = getattr(m, "__version__", "?")
            print(ok(f"{label:<20} installed v{ver:<10} → tracks {models}"))
            found.append(label)
        except (ImportError, AttributeError):
            print(info(f"{label:<20} not installed  (will be tracked when installed)"))
    return found


# ── Network checks ────────────────────────────────────────────────────────────

def _get(url: str, headers: dict, timeout: float = 5.0) -> tuple[int, dict]:
    req = urllib_request.Request(url, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        return e.code, body
    except URLError as e:
        raise ConnectionError(str(e)) from e


def _post(url: str, payload: dict, headers: dict, timeout: float = 5.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib_request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        **headers,
    }, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        return e.code, body
    except URLError as e:
        raise ConnectionError(str(e)) from e


# ── Main diagnostic ───────────────────────────────────────────────────────────

def diagnose(url: str | None = None, token: str | None = None) -> int:
    """Run full diagnostic. Returns 0 for pass, 1 for any failure."""
    print(_c("1;36", "\n╔══════════════════════════════════════╗"))
    print(_c("1;36",   "║    Modus SDK Diagnostic       ║"))
    print(_c("1;36",   "╚══════════════════════════════════════╝"))

    from modus.agent import __version__
    print(f"\n  SDK version: {__version__}   Python: {sys.version.split()[0]}\n")

    errors: list[str] = []

    # ── 1. Environment ────────────────────────────────────────────────────────
    print(head("1. Environment Variables"))

    url   = url   or os.getenv("MODUS_URL",
                                os.getenv("MODUS_ORCHESTRATOR_URL", ""))
    token = token or os.getenv("MODUS_TEAM_TOKEN", "")
    disabled = os.getenv("MODUS_DISABLED", "").lower() in ("true", "1", "yes")
    env   = os.getenv("MODUS_ENVIRONMENT", "production")
    app_id = os.getenv("MODUS_APP_ID", "(auto-detect)")
    fail_open = os.getenv("MODUS_FAIL_OPEN", "true")

    if disabled:
        print(warn("MODUS_DISABLED=true — agent will not start. Unset to enable."))

    if url:
        print(ok(f"MODUS_URL        = {url}"))
    else:
        print(fail("MODUS_URL          not set"))
        errors.append("MODUS_URL is not set")

    if token:
        masked = token[:16] + "…" + token[-4:] if len(token) > 20 else token[:8] + "…"
        print(ok(f"MODUS_TEAM_TOKEN = {masked}"))
    else:
        print(fail("MODUS_TEAM_TOKEN   not set"))
        errors.append("MODUS_TEAM_TOKEN is not set")

    print(info(f"MODUS_ENVIRONMENT  = {env}"))
    print(info(f"MODUS_APP_ID       = {app_id}"))
    print(info(f"MODUS_FAIL_OPEN    = {fail_open}"))

    # ── 2. AI SDKs ────────────────────────────────────────────────────────────
    print(head("2. AI Provider SDKs"))
    found_providers = _check_providers()
    if not found_providers:
        print(warn("No AI SDKs found. Install anthropic, openai, etc. to enable auto-tracking."))
    else:
        print(info(f"{len(found_providers)} provider(s) will be auto-instrumented on start."))

    # ── 3. Network ────────────────────────────────────────────────────────────
    print(head("3. Network Connectivity"))

    if not url:
        print(warn("Skipping network check — MODUS_URL not set."))
    else:
        # Health check
        try:
            t = time.perf_counter()
            status, body = _get(f"{url}/health", headers={}, timeout=5.0)
            ms = int((time.perf_counter() - t) * 1000)
            if status == 200:
                svc = body.get("service", "modus")
                sver = body.get("version", "?")
                print(ok(f"Orchestrator reachable   {url}/health  [{ms}ms]  service={svc} v{sver}"))
            else:
                print(warn(f"Health check returned HTTP {status}  [{ms}ms]"))
        except ConnectionError as e:
            print(fail(f"Cannot reach orchestrator: {e}"))
            errors.append(f"Network: {e}")

    # ── 4. Token + Registration ───────────────────────────────────────────────
    print(head("4. Team Token & Self-Registration"))

    if not url or not token:
        print(warn("Skipping registration check — URL or token missing."))
    else:
        try:
            # Discover app identity the same way the agent does
            try:
                from modus.discovery import EnvironmentScanner
                snap = EnvironmentScanner().scan(
                    app_id=None, app_name=None,
                    environment=env, agent_version=__version__
                )
                test_app_id = f"diag-{snap.app_id}"
                test_app_name = f"[Diagnostic] {snap.app_name}"
            except Exception:
                test_app_id = "modus-diagnose"
                test_app_name = "[Diagnostic] modus-diagnose"

            t = time.perf_counter()
            status, body = _post(
                f"{url}/api/v1/self-register",
                payload={
                    "app_id": test_app_id,
                    "app_name": test_app_name,
                    "environment": "diagnose",
                    "agent_version": __version__,
                },
                headers={"X-Modus-TeamToken": token},
                timeout=8.0,
            )
            ms = int((time.perf_counter() - t) * 1000)

            if status == 200:
                action = "registered" if body.get("registered") else "reconnected"
                team = body.get("team_slug", "?")
                app  = body.get("app_id", "?")
                print(ok(f"Token valid  →  {action} as '{app}' in team '{team}'  [{ms}ms]"))
                print(ok( "Registration successful — agent will start correctly on deploy."))
            elif status == 401:
                print(fail("Token rejected (HTTP 401) — check MODUS_TEAM_TOKEN."))
                errors.append("Team token is invalid or revoked")
            elif status == 403:
                print(fail("Token forbidden (HTTP 403) — token may be expired or team inactive."))
                errors.append("Team token forbidden")
            else:
                detail = body.get("detail", body)
                print(warn(f"Registration returned HTTP {status}: {detail}"))
        except ConnectionError as e:
            print(fail(f"Registration request failed: {e}"))
            errors.append(f"Registration network error: {e}")

    # ── 5. Pricing sanity ─────────────────────────────────────────────────────
    print(head("5. Pricing Table"))
    try:
        from modus.pricing import estimate_cost
        ic, oc, tc = estimate_cost("anthropic", "claude-sonnet-4-6", 1000, 500)
        print(ok(f"Pricing table loaded  — e.g. claude-sonnet-4-6 1k+500 tokens = ${tc:.6f}"))
        ic2, oc2, tc2 = estimate_cost("openai", "gpt-4o", 1000, 500)
        print(ok(f"                      — e.g. gpt-4o 1k+500 tokens = ${tc2:.6f}"))
    except Exception as e:
        print(fail(f"Pricing table error: {e}"))
        errors.append(f"Pricing: {e}")

    # ── 6. Import order warning ───────────────────────────────────────────────
    print(head("6. Import Order Check"))
    already_imported = []
    for pkg in ("anthropic", "openai", "boto3", "groq", "mistralai", "cohere"):
        if pkg in sys.modules:
            already_imported.append(pkg)

    if already_imported:
        print(warn(
            f"These SDKs were imported before modus: {', '.join(already_imported)}\n"
            f"       If this happened in your app (not just here), those calls won't be tracked.\n"
            f"       Fix: move 'import modus' to line 1 of your entry point."
        ))
    else:
        print(ok("No AI SDKs imported before this check — import order is fine."))

    # ── Summary ───────────────────────────────────────────────────────────────
    print(_c("1", "\n" + "─" * 46))
    if not errors:
        print(_c("32;1", "  ✓  All checks passed. Modus is ready to deploy.\n"))
        return 0
    else:
        print(_c("31;1", f"  ✗  {len(errors)} issue(s) found:\n"))
        for e in errors:
            print(_c("31", f"       • {e}"))
        print()
        return 1


# ── Policy CLI (Budget-as-Code) ──────────────────────────────────────────────

def _load_yaml(path: str) -> dict:
    """Load a YAML file using stdlib (no PyYAML dependency)."""
    # Try PyYAML first (if installed), then fall back to JSON
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            print(fail(f"Expected a YAML mapping, got {type(data).__name__}"))
            sys.exit(1)
        return data
    except ImportError:
        pass

    # Fallback: try JSON (YAML is a superset of JSON)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(fail(f"Expected a JSON object, got {type(data).__name__}"))
            sys.exit(1)
        return data
    except (json.JSONDecodeError, ValueError):
        print(fail("Cannot parse file. Install PyYAML for YAML support: pip install pyyaml"))
        print(info("Or use JSON format instead."))
        sys.exit(1)


def _dump_yaml(data: dict) -> str:
    """Dump dict to YAML string."""
    try:
        import yaml
        return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except ImportError:
        return json.dumps(data, indent=2, ensure_ascii=False)


def _admin_headers(api_key: str, token: str | None) -> dict[str, str]:
    """Auth headers for the admin policy endpoints.

    The master key (``MODUS_MASTER_API_KEY``) is sent as ``X-Modus-APIKey`` and
    is accepted in both stub and jwt auth modes. Otherwise an explicit
    ``--token`` is sent as a bearer JWT. With neither, no credentials are sent
    (works only against a stub-mode development orchestrator).
    """
    if api_key:
        return {"X-Modus-APIKey": api_key}
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def policy_validate(path: str) -> int:
    """Validate a modus-policy.yaml file. Returns 0 on success."""
    from modus.policy_schema import validate_policy_file

    print(head("Modus Policy — Validate"))
    print(info(f"File: {path}\n"))

    data = _load_yaml(path)
    errors = validate_policy_file(data)

    if errors:
        print(fail(f"{len(errors)} validation error(s):\n"))
        for e in errors:
            print(_c("31", f"  • {e}"))
        print()
        return 1

    count = len(data.get("policies", []))
    print(ok(f"Valid — {count} {'policy' if count == 1 else 'policies'} defined.\n"))
    return 0


def policy_plan(path: str, url: str | None = None, token: str | None = None) -> int:
    """Show diff between desired (YAML) and current (orchestrator) policies."""
    from modus.policy_schema import validate_policy_file

    print(head("Modus Policy — Plan"))
    print(info(f"File: {path}\n"))

    data = _load_yaml(path)
    errors = validate_policy_file(data)
    if errors:
        print(fail(f"{len(errors)} validation error(s) — fix before planning."))
        for e in errors:
            print(_c("31", f"  • {e}"))
        return 1

    url = (url or os.environ.get("MODUS_URL", "")).rstrip("/")
    api_key = os.environ.get("MODUS_MASTER_API_KEY", "")

    if not url:
        print(fail("MODUS_URL not set."))
        return 1

    headers = _admin_headers(api_key, token)

    # Fetch current policies from orchestrator
    try:
        status, current = _get(f"{url}/api/v1/policies", headers)
    except ConnectionError as e:
        print(fail(f"Cannot reach orchestrator: {e}"))
        return 1

    if status != 200:
        print(fail(f"GET /api/v1/policies returned {status}"))
        return 1

    # Build name→policy maps
    current_policies = current if isinstance(current, list) else current.get("policies", [])
    current_by_name: dict[str, dict] = {}
    for p in current_policies:
        current_by_name[p.get("name", "")] = p

    desired = data.get("policies", [])
    defaults = data.get("defaults", {})

    adds = []
    changes = []
    unchanged = []

    for dp in desired:
        name = dp["name"]
        if name not in current_by_name:
            adds.append(dp)
        else:
            cp = current_by_name[name]
            diffs = _policy_diff(dp, cp, defaults)
            if diffs:
                changes.append((name, diffs))
            else:
                unchanged.append(name)

    # Policies in current but not in desired
    desired_names = {p["name"] for p in desired}
    removes = [n for n in current_by_name if n not in desired_names]

    # Print plan
    print()
    if adds:
        print(_c("32", f"  + {len(adds)} to add:"))
        for p in adds:
            print(f"      + {p['name']} ({p['type']})")
    if changes:
        print(_c("33", f"  ~ {len(changes)} to update:"))
        for name, diffs in changes:
            print(f"      ~ {name}")
            for d in diffs:
                print(f"          {d}")
    if removes:
        print(_c("31", f"  - {len(removes)} to remove:"))
        for n in removes:
            print(f"      - {n}")
    if unchanged:
        print(_c("36", f"  = {len(unchanged)} unchanged"))

    total = len(adds) + len(changes) + len(removes)
    if total == 0:
        print(ok("No changes needed — current state matches desired.\n"))
    else:
        print(f"\n  Plan: {len(adds)} add, {len(changes)} change, {len(removes)} remove\n")
        print(info("Run 'modus policy apply' to apply these changes.\n"))

    return 0


# What POST /api/v1/policies/apply assumes for fields a policy file omits.
_SERVER_DEFAULTS = {"scope": "team", "effect": "deny", "priority": 100}


def _policy_diff(desired: dict, current: dict, defaults: dict) -> list[str]:
    """Compare desired vs current policy, return list of diff descriptions."""
    diffs = []
    compare_fields = ["type", "scope", "effect", "priority", "config", "conditions",
                      "action", "suggested_model"]
    for field in compare_fields:
        d_val = desired.get(field, defaults.get(field))
        if d_val is None:
            # Omitted in the file: the server applies its documented default.
            d_val = _SERVER_DEFAULTS.get(field)
        c_val = current.get(field)
        if field == "type" and c_val is None:
            # GET /api/v1/policies names this field ``policy_type``.
            c_val = current.get("policy_type")
        # Normalize None vs missing
        if d_val is None and c_val is None:
            continue
        if d_val != c_val:
            diffs.append(f"{field}: {c_val!r} → {d_val!r}")
    # enabled vs is_active
    d_enabled = desired.get("enabled", True)
    c_active = current.get("is_active", True)
    if d_enabled != c_active:
        diffs.append(f"enabled: {c_active} → {d_enabled}")
    return diffs


def policy_apply(path: str, url: str | None = None, token: str | None = None,
                 dry_run: bool = False) -> int:
    """Apply policies from YAML to orchestrator."""
    from modus.policy_schema import validate_policy_file

    print(head("Modus Policy — Apply"))
    print(info(f"File: {path}\n"))

    data = _load_yaml(path)
    errors = validate_policy_file(data)
    if errors:
        print(fail(f"{len(errors)} validation error(s) — fix before applying."))
        for e in errors:
            print(_c("31", f"  • {e}"))
        return 1

    url = (url or os.environ.get("MODUS_URL", "")).rstrip("/")
    api_key = os.environ.get("MODUS_MASTER_API_KEY", "")

    if not url:
        print(fail("MODUS_URL not set."))
        return 1

    headers = _admin_headers(api_key, token)

    if dry_run:
        print(info("Dry run — no changes will be applied.\n"))

    # POST to batch apply endpoint
    payload = {
        "version": data.get("version", "1"),
        "policies": data.get("policies", []),
        "defaults": data.get("defaults"),
        "dry_run": dry_run,
    }

    try:
        status, result = _post(f"{url}/api/v1/policies/apply", payload, headers)
    except ConnectionError as e:
        print(fail(f"Cannot reach orchestrator: {e}"))
        return 1

    if status not in (200, 201):
        detail = result.get("detail", result)
        if isinstance(detail, dict) and detail.get("errors"):
            print(fail(f"Apply rejected ({status}): {detail.get('message', 'invalid policy file')}"))
            for e in detail["errors"]:
                print(_c("31", f"  • {e}"))
        else:
            print(fail(f"Apply failed ({status}): {detail}"))
        return 1

    # Print results
    summary = result.get("summary", {})
    created = summary.get("created", 0)
    updated = summary.get("updated", 0)
    removed = summary.get("removed", 0)
    unchanged = summary.get("unchanged", 0)
    errs = result.get("errors", [])

    if dry_run:
        print(ok(f"Dry run: {created} add, {updated} update, {removed} remove, {unchanged} unchanged"))
    else:
        print(ok(f"Applied: {created} created, {updated} updated, {removed} removed, "
                 f"{unchanged} unchanged"))

    if errs:
        print(warn(f"{len(errs)} error(s):"))
        for e in errs:
            print(_c("31", f"  • {e}"))

    print()
    return 0 if not errs else 1


def policy_export(url: str | None = None, token: str | None = None,
                  scope: str | None = None) -> int:
    """Export current policies from orchestrator as YAML."""
    print(head("Modus Policy — Export"))

    url = (url or os.environ.get("MODUS_URL", "")).rstrip("/")
    api_key = os.environ.get("MODUS_MASTER_API_KEY", "")

    if not url:
        print(fail("MODUS_URL not set."), file=sys.stderr)
        return 1

    headers = _admin_headers(api_key, token)

    endpoint = f"{url}/api/v1/policies/export"
    if scope:
        endpoint += f"?scope={scope}"

    try:
        status, result = _get(endpoint, headers)
    except ConnectionError as e:
        print(fail(f"Cannot reach orchestrator: {e}"), file=sys.stderr)
        return 1

    if status != 200:
        detail = result.get("detail", result)
        print(fail(f"Export failed ({status}): {detail}"), file=sys.stderr)
        return 1

    # Output YAML to stdout (pipe-friendly)
    yaml_str = _dump_yaml(result)
    sys.stdout.write(yaml_str)
    return 0


def main() -> None:
    import argparse

    # The CLI prints Unicode symbols; never crash on a legacy console codepage
    # (e.g. cp1252 on Windows) -- substitute unencodable characters instead.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="modus",
        description="Modus SDK — AI cost governance CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  diagnose           Run SDK diagnostics
  policy validate    Validate a modus-policy.yaml file
  policy plan        Show diff between YAML and current state
  policy apply       Apply YAML policies to orchestrator
  policy export      Export current policies as YAML

Examples:
  python -m modus diagnose
  python -m modus policy validate modus-policy.yaml
  python -m modus policy plan modus-policy.yaml
  python -m modus policy apply modus-policy.yaml --dry-run
  python -m modus policy export > modus-policy.yaml
        """,
    )

    subparsers = parser.add_subparsers(dest="command")

    # diagnose
    diag = subparsers.add_parser("diagnose", help="Run SDK diagnostics")
    diag.add_argument("--url",   default=None, help="Override MODUS_URL")
    diag.add_argument("--token", default=None, help="Override MODUS_TEAM_TOKEN")

    # policy
    policy_parser = subparsers.add_parser("policy", help="Budget-as-Code policy management")
    policy_sub = policy_parser.add_subparsers(dest="policy_command")

    # policy validate
    pv = policy_sub.add_parser("validate", help="Validate a policy YAML file")
    pv.add_argument("file", help="Path to modus-policy.yaml")

    # policy plan
    pp = policy_sub.add_parser("plan", help="Show diff between YAML and current state")
    pp.add_argument("file", help="Path to modus-policy.yaml")
    pp.add_argument("--url",   default=None, help="Override MODUS_URL")
    pp.add_argument("--token", default=None, help="Bearer JWT (jwt auth mode). MODUS_MASTER_API_KEY takes precedence")

    # policy apply
    pa = policy_sub.add_parser("apply", help="Apply policies to orchestrator")
    pa.add_argument("file", help="Path to modus-policy.yaml")
    pa.add_argument("--url",   default=None, help="Override MODUS_URL")
    pa.add_argument("--token", default=None, help="Bearer JWT (jwt auth mode). MODUS_MASTER_API_KEY takes precedence")
    pa.add_argument("--dry-run", action="store_true", help="Preview changes without applying")

    # policy export
    pe = policy_sub.add_parser("export", help="Export current policies as YAML")
    pe.add_argument("--url",   default=None, help="Override MODUS_URL")
    pe.add_argument("--token", default=None, help="Bearer JWT (jwt auth mode). MODUS_MASTER_API_KEY takes precedence")
    pe.add_argument("--scope", default=None, choices=["platform", "team", "app"],
                    help="Filter by scope")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "diagnose":
        sys.exit(diagnose(url=args.url, token=args.token))

    elif args.command == "policy":
        if args.policy_command is None:
            policy_parser.print_help()
            sys.exit(0)
        elif args.policy_command == "validate":
            sys.exit(policy_validate(args.file))
        elif args.policy_command == "plan":
            sys.exit(policy_plan(args.file, url=args.url, token=args.token))
        elif args.policy_command == "apply":
            sys.exit(policy_apply(args.file, url=args.url, token=args.token,
                                  dry_run=args.dry_run))
        elif args.policy_command == "export":
            sys.exit(policy_export(url=args.url, token=args.token, scope=args.scope))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
