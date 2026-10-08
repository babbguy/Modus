"""bcrypt must not run on the event loop.

A cost-12 bcrypt hash takes ~0.4 s of CPU on a 1-vCPU server. Run inline in an
async handler it stalls every other request for that long (concurrent app
registrations then time out in the SDK). The hashing must happen in a worker
thread so the event loop keeps serving requests.
"""
from __future__ import annotations

import asyncio
import time

import pytest

BLOCK_S = 0.4  # a stand-in for one slow bcrypt call


@pytest.fixture(autouse=True)
def _no_write_queue(monkeypatch):
    """Registration hands its DB writes to the write queue; collect them instead."""
    from orchestrator.core import write_queue

    async def _enqueue(item):
        return None

    monkeypatch.setattr(write_queue, "enqueue", _enqueue)
    monkeypatch.setattr(write_queue, "queue_over_pressure", lambda: False)


def _slow(result):
    def _fn(*args, **kwargs):
        time.sleep(BLOCK_S)  # blocks whichever thread runs it
        return result
    return _fn


async def _longest_loop_stall(coro) -> float:
    """Run ``coro`` while a ticker measures the longest gap between its ticks."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        done.set()
        await task
    return result, max(gaps or [0.0])


async def test_app_registration_hashes_off_the_event_loop(client, monkeypatch):
    from orchestrator.api import apps

    monkeypatch.setattr(apps, "_verify_master_key", lambda key: True)
    monkeypatch.setattr(apps, "_hash_key", _slow("$2b$12$" + "x" * 53))
    resp, stall = await _longest_loop_stall(client.post("/api/v1/apps/register", json={
        "app_id": "loop-check", "app_name": "Loop check", "team_slug": "loop-team",
    }))
    assert resp.status_code == 201, resp.text
    assert stall < BLOCK_S * 0.75, f"event loop stalled {stall:.2f}s during registration"


async def test_team_token_generation_hashes_off_the_event_loop(client, monkeypatch):
    from orchestrator.api import topology

    team = (await client.post("/api/v1/teams", json={"slug": "loop-tok", "name": "Loop"})).json()
    monkeypatch.setattr(topology, "_verify_master_key", lambda key: True)
    monkeypatch.setattr(topology, "_hash_team_token", _slow("$2b$08$" + "x" * 53))
    resp, stall = await _longest_loop_stall(
        client.post(f"/api/v1/teams/{team['id']}/registration-token", json={})
    )
    assert resp.status_code == 201, resp.text
    assert stall < BLOCK_S * 0.75, f"event loop stalled {stall:.2f}s during token generation"


@pytest.mark.parametrize("path", [
    "orchestrator/api/apps.py", "orchestrator/api/topology.py", "orchestrator/api/users.py",
])
def test_no_inline_bcrypt_in_async_handlers(path):
    """Every bcrypt hash/check in these handlers goes through asyncio.to_thread."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    slow = {"hashpw", "checkpw", "_hash_key", "_hash_team_token"}
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in slow:
                    offenders.append(f"{fn.name}:{node.lineno} {name}()")
    assert not offenders, "call these via asyncio.to_thread: " + ", ".join(offenders)
