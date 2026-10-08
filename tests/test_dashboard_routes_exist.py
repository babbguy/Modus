"""
Tests -- every /api/v1/ path the dashboard JavaScript calls must exist in the app.

Scans dashboard/js/**/*.js for string/template literals starting with
"/api/v1/" (and apiFetch()/apiPost() paths, which are relative to
/api/v1/dashboard/), turns ${...} into a wildcard and matches them against the
FastAPI route table. Method mismatches are only checked for the calls whose
method is spelled out next to the path.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DASH_JS = Path(__file__).resolve().parent.parent / "dashboard" / "js"


@pytest.fixture(scope="module")
def routes():
    from orchestrator.main import create_app

    app = create_app()
    out = []
    for r in app.routes:
        path = getattr(r, "path", None)
        if path and path.startswith("/api/"):
            rx = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "/?$")
            out.append((getattr(r, "methods", None) or set(), rx))
    return out


# Paths built from a variable final segment: expand to the values the code uses.
_EXPANSIONS = {
    "/api/v1/admin/federation/peers/x/x": [
        "/api/v1/admin/federation/peers/x/pause",
        "/api/v1/admin/federation/peers/x/resume",
    ],
}
# views/finance.js wraps calls as _financeFetch('summary') / _financePost('forecast', ...).
_WRAPPER_BASES = {"/api/v1/finance/x": "/api/v1/finance/"}


def _dashboard_paths():
    found: dict[tuple[str, str | None], str] = {}
    for fp in sorted(DASH_JS.rglob("*.js")):
        lines = fp.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines, 1):
            where = f"{fp.relative_to(DASH_JS)}:{i}"
            for m in re.finditer(r"""[`'"](/api/v1/[^`'"\s?]*)""", line):
                raw = re.sub(r"\$\{[^}]*\}", "x", m.group(1))
                if "(" in raw or ")" in raw:      # prose in a comment, not a path
                    continue
                ctx = "\n".join(lines[i - 1:i + 3])
                mm = re.search(r"method:\s*['\"](GET|POST|PUT|DELETE|PATCH)['\"]", ctx)
                if raw in _WRAPPER_BASES:
                    continue
                for expanded in _EXPANSIONS.get(raw, [raw]):
                    found.setdefault((expanded, mm.group(1) if mm else None), where)
            for m in re.finditer(r"""_finance(?:Fetch|Post)\(\s*['"]([^'"?]+)""", line):
                found.setdefault(("/api/v1/finance/" + m.group(1), None), where)
            for m in re.finditer(r"""(?:apiFetch|apiPost)\(\s*[`'"]([^`'"]+)[`'"]""", line):
                raw = "/api/v1/dashboard/" + re.sub(r"\$\{[^}]*\}", "x", m.group(1)).split("?")[0]
                found.setdefault((raw, None), where)
    return found


def test_every_dashboard_api_path_exists(routes):
    problems = []
    for (path, method), where in _dashboard_paths().items():
        matching = [methods for methods, rx in routes if rx.match(path)]
        if not matching:
            problems.append(f"{path} (no such route) at {where}")
        elif method and not any(method in m for m in matching):
            problems.append(f"{method} {path} (route allows {sorted(set().union(*matching))}) at {where}")
    assert not problems, "dashboard calls routes that do not exist:\n" + "\n".join(problems)


def test_scanner_finds_the_calls_it_is_meant_to_police():
    paths = {p for p, _ in _dashboard_paths()}
    assert "/api/v1/audit-log?limit=100".split("?")[0] in paths
    assert any(p.startswith("/api/v1/admin/federation/peers/x/test") for p in paths)
    assert "/api/v1/audit" not in paths
    assert "/api/v1/admin/federation/peers/test-connection" not in paths
