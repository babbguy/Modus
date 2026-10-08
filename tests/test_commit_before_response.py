"""Writes must be committed before the response is sent.

FastAPI runs the teardown of a ``yield`` dependency after the response has been
sent unless the dependency is declared with ``scope="function"``. get_session
commits in its teardown, so every route that uses it must declare that scope;
otherwise clients see 201/200 for writes that are not yet committed (or that
then fail to commit).
"""
from __future__ import annotations

import asyncio
import threading
import time
import urllib.request

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from orchestrator.db.session import get_session
from orchestrator.main import app


def _session_dependants(dependant, path=()):
    for dep in dependant.dependencies:
        if dep.call is get_session:
            yield path, dep
        yield from _session_dependants(dep, path + (getattr(dep.call, "__name__", "?"),))


def test_every_route_commits_before_responding():
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    checked, unscoped = 0, []
    for route in routes:
        for via, dep in _session_dependants(route.dependant):
            checked += 1
            if dep.scope != "function":
                unscoped.append(f"{sorted(route.methods)} {route.path} via {' > '.join(via) or 'handler'}")
    assert checked > 100, "expected the write session on most routes"
    assert not unscoped, "get_session must use scope='function':\n" + "\n".join(unscoped)


def test_function_scope_finishes_teardown_before_the_response():
    """Guards the FastAPI behaviour the rule above relies on, on a real server."""
    uvicorn = pytest.importorskip("uvicorn")
    events: dict[str, float] = {}
    demo = FastAPI()

    async def slow_commit():
        yield None
        await asyncio.sleep(0.3)
        events["committed"] = time.perf_counter()

    @demo.post("/write")
    async def write(_=Depends(slow_commit, scope="function")):
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(demo, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/write", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        responded = time.perf_counter()
        assert "committed" in events and events["committed"] <= responded
    finally:
        server.should_exit = True
        thread.join(timeout=5)
