"""
SDK route discovery for FastAPI apps that include routers.

FastAPI 0.136 and earlier copy every included route into ``app.routes``.
FastAPI 0.137 and later keep each included router as a single node in
``app.routes`` and build its routes lazily, so a flat walk of ``app.routes``
misses them. CI installs one FastAPI version, so the lazy shape is also built
directly here, modelled on FastAPI's own classes (0.137.0 to 0.143.1): the node
has ``effective_route_contexts()``, which yields the routes of the router and
of its nested routers with the effective (prefixed) ``path``; a WebSocket or
plain Starlette route is carried as ``starlette_route``.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, WebSocket

from modus.discovery import _extract_fastapi_routes


def _paths(routes: list[dict]) -> list[tuple[str, list[str]]]:
    return sorted((r["path"], r["methods"]) for r in routes)


# ── Real FastAPI (whichever version is installed) ────────────────────────────


def _app_with_nested_routers() -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/health")
    def health():
        return {}

    inner = APIRouter()

    @inner.post("/chat")
    def chat():
        """Answer a chat message.

        Calls the model."""
        return {}

    @inner.websocket("/stream")
    async def stream(ws: WebSocket):
        await ws.close()

    middle = APIRouter(prefix="/v1")
    middle.include_router(inner, prefix="/ai")

    @middle.get("/models")
    def models():
        return {}

    app.include_router(middle)
    return app


def test_real_app_reports_routes_in_nested_included_routers():
    routes = _extract_fastapi_routes(_app_with_nested_routers())

    assert _paths(routes) == [
        ("/health", ["GET"]),
        ("/v1/ai/chat", ["POST"]),
        ("/v1/ai/stream", ["GET"]),
        ("/v1/models", ["GET"]),
    ]
    chat = next(r for r in routes if r["path"] == "/v1/ai/chat")
    assert chat["name"] == "chat"
    assert chat["likely_ai_endpoint"] is True
    assert chat["docstring"] == "Answer a chat message."


# ── FastAPI 0.137+ lazy shape, built directly ────────────────────────────────


def _endpoint():
    """Generate a completion."""


class _EffectiveRouteContext:
    def __init__(self, path, methods, name, endpoint=None, starlette_route=None):
        self.path = path
        self.methods = methods
        self.name = name
        self.endpoint = endpoint
        self.starlette_route = starlette_route


class _Route:
    def __init__(self, path, methods, name):
        self.path = path
        self.methods = methods
        self.name = name
        self.endpoint = None


def _lazy_contexts():
    # What FastAPI yields for an included router that holds an API route and a
    # nested included router with an API route, a plain Starlette route and a
    # WebSocket route. FastAPI flattens nested routers and applies prefixes.
    return [
        _EffectiveRouteContext("/v1/models", {"GET"}, "models"),
        _EffectiveRouteContext("/v1/ai/chat", {"POST"}, "chat", endpoint=_endpoint),
        _EffectiveRouteContext(
            "", set(), "",
            starlette_route=_Route("/v1/ai/legacy", {"GET", "HEAD"}, "legacy"),
        ),
        _EffectiveRouteContext(
            "", set(), "",
            starlette_route=_Route("/v1/ai/stream", None, "stream"),
        ),
    ]


class _IncludedRouter:
    """An included-router node as FastAPI 0.137+ stores it."""

    path = None
    methods = None
    name = None

    def effective_route_contexts(self):
        yield from _lazy_contexts()


class _UnknownNode:
    """A route-table node with no path that discovery cannot expand."""

    path = None
    methods = None
    name = None


class _App:
    def __init__(self, routes):
        self.routes = routes


EXPECTED_LAZY = [
    ("/health", ["GET"]),
    ("/v1/ai/chat", ["POST"]),
    ("/v1/ai/legacy", ["GET", "HEAD"]),
    ("/v1/ai/stream", ["GET"]),
    ("/v1/models", ["GET"]),
]


def test_lazy_included_router_node_is_expanded():
    app = _App([_Route("/health", {"GET"}, "health"), _IncludedRouter()])

    routes = _extract_fastapi_routes(app)

    assert _paths(routes) == EXPECTED_LAZY
    chat = next(r for r in routes if r["path"] == "/v1/ai/chat")
    assert chat["name"] == "chat"
    assert chat["likely_ai_endpoint"] is True
    assert chat["docstring"] == "Generate a completion."


def test_route_without_a_path_is_skipped_not_reported():
    # A node discovery cannot expand must never appear as a route.
    app = _App([_Route("/health", {"GET"}, "health"), _UnknownNode()])

    routes = _extract_fastapi_routes(app)

    assert _paths(routes) == [("/health", ["GET"])]
