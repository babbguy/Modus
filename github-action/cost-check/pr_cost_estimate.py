#!/usr/bin/env python3
"""
Modus — CI PR cost-impact estimator
===================================

Collects the AI model references changed in a pull request, asks a Modus
orchestrator to estimate the monthly cost delta, and emits a Markdown comment
body for the CI job to post on the PR.

Zero third-party dependencies — Python standard library only (urllib, json,
re, argparse). This mirrors Modus Law #1 (stdlib only) and keeps the action
runnable on any GitHub-hosted or self-hosted runner without `pip install`.

Two ways to collect model references (documented heuristic):

  1. Diff scan (default): scan the PR's unified diff. For each file, model
     identifiers on removed (`-`) lines are treated as the *current* model and
     those on added (`+`) lines as the *proposed* model. A file that both
     removes and adds a model is reported as a `current -> proposed` swap
     (paired by order of appearance); an added model with no removed
     counterpart is reported as a brand-new model.

  2. Explicit list (`--models`): a comma/newline separated list of model
     identifiers, used verbatim as proposed models with no current model.
     Useful when the change is not a simple in-file string swap.

The heuristic is intentionally simple and is clearly labelled "estimated" in
the output. It never claims precision it does not have.

Usage (inside action.yml):

    python pr_cost_estimate.py \
        --api-url "$MODUS_API_URL" \
        --api-key "$MODUS_API_KEY" \
        --app-id "$MODUS_APP_ID" \
        --pr-number "$PR_NUMBER" \
        --pr-url "$PR_URL" \
        --diff-file pr.diff \
        --models "gpt-4o-mini" \
        --output-comment comment.md \
        --github-output "$GITHUB_OUTPUT"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Optional

# ── Known provider / model identifier heuristic ───────────────────────────────
# A deliberately conservative regex matching common provider model families.
# Extend as new families ship. Matching quoted/bare identifiers on diff lines.
MODEL_RE = re.compile(
    r"\b("
    r"gpt-[0-9][\w.\-]*"           # gpt-4, gpt-4o, gpt-4o-mini, gpt-3.5-turbo
    r"|o[1-9](?:-[\w.\-]+)?"        # o1, o1-mini, o3, o3-mini
    r"|text-embedding-[\w.\-]+"     # text-embedding-3-small, ...-ada-002
    r"|claude-[\w.\-]+"             # claude-3-5-sonnet-20241022, claude-sonnet-4
    r"|gemini-[\w.\-]+"             # gemini-1.5-pro, gemini-2.0-flash
    r"|(?:meta-)?llama-?[0-9][\w.\-]*"  # llama-3, llama3-70b, meta-llama-3
    r"|mi(?:stral|xtral)-[\w.\-]+"  # mistral-large, mixtral-8x7b
    r")\b",
    re.IGNORECASE,
)

MAX_CHANGES = 50  # matches PrCostRequest.model_changes max_length


def _find_models(line: str) -> list[str]:
    """Return every known model identifier appearing in a single line, in order."""
    return [m.group(1) for m in MODEL_RE.finditer(line)]


def extract_changes_from_diff(diff_text: str) -> list[dict]:
    """
    Parse a unified diff into a list of model-change dicts:
        {"file_path": str, "current_model": Optional[str], "proposed_model": str}

    Heuristic: per file, models on removed lines are candidate current models
    and models on added lines are candidate proposed models. They are paired by
    order of appearance (removed[i] -> added[i]); leftover added models with no
    removed counterpart are reported with current_model=None.
    """
    current_file: Optional[str] = None
    removed: dict[str, list[str]] = {}
    added: dict[str, list[str]] = {}
    order: list[str] = []  # preserve file discovery order

    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            # e.g. "+++ b/src/ai.py"  or  "+++ /dev/null"
            path = raw[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current_file = None if path == "/dev/null" else path
            if current_file is not None and current_file not in order:
                order.append(current_file)
                removed.setdefault(current_file, [])
                added.setdefault(current_file, [])
            continue
        if raw.startswith("--- "):
            continue  # old-file header, not a content removal
        if current_file is None:
            continue
        if raw.startswith("+"):
            added[current_file].extend(_find_models(raw[1:]))
        elif raw.startswith("-"):
            removed[current_file].extend(_find_models(raw[1:]))

    changes: list[dict] = []
    for path in order:
        adds = added.get(path, [])
        dels = removed.get(path, [])
        for i, proposed in enumerate(adds):
            current = dels[i] if i < len(dels) else None
            # Skip no-op lines where the same model appears unchanged.
            if current is not None and current == proposed:
                continue
            changes.append({
                "file_path": path,
                "current_model": current,
                "proposed_model": proposed,
            })
    return _dedupe(changes)


def parse_explicit_models(models_input: Optional[str]) -> list[dict]:
    """Parse a comma/newline separated list of model ids into change dicts."""
    if not models_input:
        return []
    tokens = re.split(r"[,\n]", models_input)
    changes = []
    for tok in tokens:
        model = tok.strip()
        if model:
            changes.append({
                "file_path": None,
                "current_model": None,
                "proposed_model": model,
            })
    return _dedupe(changes)


def _dedupe(changes: list[dict]) -> list[dict]:
    """Drop exact duplicate (file, current, proposed) triples, preserve order."""
    seen = set()
    out = []
    for c in changes:
        key = (c.get("file_path"), c.get("current_model"), c["proposed_model"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def build_payload(
    app_id: str,
    pr_number: int,
    pr_url: Optional[str],
    changes: list[dict],
) -> dict:
    """Assemble the POST body for /api/v1/ci/pr-cost-estimate."""
    payload = {
        "app_id": app_id,
        "pr_number": int(pr_number),
        "model_changes": changes[:MAX_CHANGES],
    }
    if pr_url:
        payload["pr_url"] = pr_url
    return payload


def format_delta(amount: float) -> str:
    """Format a signed USD monthly delta, e.g. '+$890.00', '-$142.00', '$0.00'."""
    a = round(float(amount), 2)
    if a > 0:
        return f"+${a:,.2f}"
    if a < 0:
        return f"-${abs(a):,.2f}"
    return "$0.00"


def format_comment(response: dict) -> str:
    """
    Build the Markdown PR comment body from the endpoint response.

    The headline number is always labelled 'estimated' — the action never
    presents it as an exact figure.
    """
    total = float(response.get("total_monthly_delta_usd", 0.0))
    impacts = response.get("model_impacts", []) or []
    headline = format_delta(total)

    lines = [
        "## Modus — Estimated AI cost impact",
        "",
        f"**Estimated monthly AI cost impact of this PR: {headline}/mo**",
        "",
        "> This is an *estimate* only. It projects your last 30 days of call "
        "volume onto the model(s) changed in this PR using the current Modus "
        "pricing table. Actual cost depends on future traffic.",
        "",
    ]

    if impacts:
        lines.append("| File | Change | Est. monthly Δ |")
        lines.append("| --- | --- | --- |")
        for imp in impacts:
            fp = imp.get("file_path") or "—"
            cur = imp.get("current_model")
            prop = imp.get("proposed_model")
            change = f"`{cur}` → `{prop}`" if cur else f"(new) `{prop}`"
            delta = format_delta(imp.get("monthly_delta_usd", 0.0))
            lines.append(f"| {fp} | {change} | {delta} |")
        lines.append("")

    lines.append("<sub>Posted by the Modus CI cost check.</sub>")
    return "\n".join(lines)


def post_estimate(api_url: str, api_key: str, payload: dict, timeout: float = 30.0) -> dict:
    """POST the payload to the Modus pr-cost-estimate endpoint and return JSON."""
    url = api_url.rstrip("/") + "/api/v1/ci/pr-cost-estimate"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Modus-APIKey": api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted URL)
        return json.loads(resp.read().decode("utf-8"))


def _write_github_output(path: Optional[str], key: str, value: str) -> None:
    if not path:
        return
    # Multi-line safe via heredoc-style delimiter.
    delim = "MODUS_EOF"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{key}<<{delim}\n{value}\n{delim}\n")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Estimate PR AI cost impact via Modus.")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--pr-url", default=None)
    parser.add_argument("--diff-file", default=None, help="Path to a unified diff file.")
    parser.add_argument("--models", default=None, help="Explicit comma/newline model list.")
    parser.add_argument("--output-comment", default=None, help="Where to write the comment body.")
    parser.add_argument("--github-output", default=None, help="$GITHUB_OUTPUT file for step outputs.")
    args = parser.parse_args(argv)

    changes: list[dict] = []
    if args.diff_file:
        try:
            with open(args.diff_file, encoding="utf-8", errors="replace") as fh:
                changes.extend(extract_changes_from_diff(fh.read()))
        except FileNotFoundError:
            print(f"::warning::diff file not found: {args.diff_file}", file=sys.stderr)
    changes.extend(parse_explicit_models(args.models))
    changes = _dedupe(changes)

    if not changes:
        body = (
            "## Modus — Estimated AI cost impact\n\n"
            "No known AI model references were detected in this PR's diff, so "
            "there is no estimated cost impact to report.\n\n"
            "<sub>Posted by the Modus CI cost check.</sub>"
        )
        print("No model references detected; skipping estimate call.")
        if args.output_comment:
            with open(args.output_comment, "w", encoding="utf-8") as fh:
                fh.write(body)
        _write_github_output(args.github_output, "comment", body)
        _write_github_output(args.github_output, "total-delta-usd", "0")
        return 0

    payload = build_payload(args.app_id, args.pr_number, args.pr_url, changes)
    print("Requesting estimate for payload:")
    print(json.dumps(payload, indent=2))

    try:
        response = post_estimate(args.api_url, args.api_key, payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"::error::Modus returned HTTP {e.code}: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"::error::Could not reach Modus at {args.api_url}: {e.reason}", file=sys.stderr)
        return 1

    body = format_comment(response)
    print(body)
    if args.output_comment:
        with open(args.output_comment, "w", encoding="utf-8") as fh:
            fh.write(body)
    _write_github_output(args.github_output, "comment", body)
    _write_github_output(
        args.github_output,
        "total-delta-usd",
        str(response.get("total_monthly_delta_usd", 0.0)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
