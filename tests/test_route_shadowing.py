"""Tests -- literal routes must not be shadowed by /{id} routes declared earlier."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute


async def test_user_invitations_list_is_not_treated_as_a_user_id(client):
    resp = await client.get("/api/v1/users/invitations")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_no_literal_route_is_shadowed_by_an_earlier_parameter_route():
    from orchestrator.main import create_app

    routes = [r for r in create_app().routes if isinstance(r, APIRoute)]
    problems = []
    for i, later in enumerate(routes):
        if "{" in later.path:
            continue
        for earlier in routes[:i]:
            if "{" not in earlier.path or not (later.methods & earlier.methods):
                continue
            rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+", earlier.path) + "$"
            if re.match(rx, later.path):
                problems.append(f"{sorted(later.methods)} {later.path} is shadowed by {earlier.path}")
    assert not problems, "\n".join(problems)


def test_chargeback_cost_center_filter_has_no_untyped_null_parameter():
    """`:cc_id IS NULL` makes asyncpg raise AmbiguousParameterError (HTTP 500 on PostgreSQL)."""
    root = Path(__file__).resolve().parent.parent
    for rel in ("orchestrator/api/finance.py", "orchestrator/core/report_scheduler.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert ":cc_id IS NULL" not in src.replace("CAST(:cc_id AS TEXT) IS NULL", "")


def test_team_filter_has_no_untyped_null_parameter():
    root = Path(__file__).resolve().parent.parent
    for rel in ("orchestrator/api/finance.py", "orchestrator/api/insights.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert ":team_id IS NULL" not in src.replace("CAST(:team_id AS TEXT) IS NULL", "")
