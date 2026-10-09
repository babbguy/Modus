# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Write-then-read: create a resource, then use it on the very next request.

A response that is sent before its transaction commits makes the next request
see nothing (404, 401, or a stale allow). Unit tests share one in-process
session and cannot see this; a real server with a real database can.
"""
from __future__ import annotations

import secrets

from .common import Client, Results, log

PHASE = "write-then-read"


def run_write_then_read(admin: Client, master: Client, app_client_factory, results: Results,
                        run_label: str, iterations: int = 12) -> None:
    problems: list[str] = []
    done = 0
    for i in range(iterations):
        tag = f"{run_label}-{i}-{secrets.token_hex(2)}"
        slug = f"gate-wtr-{tag}"
        try:
            # team -> read it back -> registration token
            r = admin.post("/api/v1/teams", {"slug": slug, "name": f"Gate wtr {tag}"})
            if r.status != 201:
                problems.append(f"#{i} create team -> {r}")
                continue
            team_id = r.body["id"]
            g = admin.get(f"/api/v1/teams/{team_id}")
            if g.status != 200:
                problems.append(f"#{i} GET new team -> {g.status}")
            t = master.post(f"/api/v1/teams/{team_id}/registration-token", {})
            if not t.ok:
                problems.append(f"#{i} registration token for new team -> {t.status}")
                continue
            token = t.body["registration_token"]

            # token -> self-register -> evaluate with the new key
            reg = admin.post("/api/v1/self-register",
                             {"app_id": f"gate-wtr-app-{tag}", "app_name": f"gate-wtr-app-{tag}",
                              "environment": "production"},
                             headers={"X-Modus-TeamToken": token})
            if reg.status not in (200, 201):
                problems.append(f"#{i} self-register with new token -> {reg}")
                continue
            app = app_client_factory(reg.body["api_key"])
            ev = app.post("/api/v1/policy/evaluate", {"provider": "openai", "model": "gpt-4o"}, paced=False)
            if ev.status != 200 or ev.body.get("decision") != "allow":
                problems.append(f"#{i} evaluate with new app key -> {ev}")

            # policy -> evaluate must already see it
            model = f"gate-wtr-model-{tag}"
            p = admin.post("/api/v1/policies", {
                "name": f"gate wtr deny {tag}", "scope": "team", "team_id": team_id,
                "policy_type": "model_denylist", "effect": "deny", "config": {"models": [model]},
            })
            if p.status not in (200, 201):
                problems.append(f"#{i} create policy -> {p}")
                continue
            ev2 = app.post("/api/v1/policy/evaluate", {"provider": "openai", "model": model}, paced=False)
            if ev2.status != 200 or ev2.body.get("decision") != "deny":
                problems.append(f"#{i} evaluate right after creating a deny policy -> {ev2}")

            # admin registration (master key) -> evaluate and list right away
            app2 = f"gate-wtr-reg-{tag}"
            rr = master.post("/api/v1/apps/register", {"app_id": app2, "app_name": app2, "team_slug": slug})
            if rr.status != 201:
                problems.append(f"#{i} admin app registration -> {rr}")
                continue
            ev3 = app_client_factory(rr.body["api_key"]).post(
                "/api/v1/policy/evaluate", {"provider": "openai", "model": model}, paced=False)
            if ev3.status != 200 or ev3.body.get("decision") != "deny":
                problems.append(f"#{i} evaluate with a just-registered app key -> {ev3}")
            lst = admin.get(f"/api/v1/apps?team_id={team_id}")
            ids = {a.get("app_id") for a in (lst.body or [])} if lst.ok else set()
            if not lst.ok or app2 not in ids or f"gate-wtr-app-{tag}" not in ids:
                problems.append(f"#{i} app list right after registration -> {lst.status} {sorted(ids)}")
            done += 1
        except Exception as exc:
            problems.append(f"#{i} {type(exc).__name__}: {exc}")
    log(f"write-then-read: {done}/{iterations} rounds clean, {len(problems)} problems")
    results.equal(PHASE, f"{iterations} create-then-use rounds (team, token, app, policy): stale reads",
                  [], problems[:4] + ([f"... {len(problems) - 4} more"] if len(problems) > 4 else []))
