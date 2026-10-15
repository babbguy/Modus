"""Tests -- the Prometheus endpoint labels its body with the format it is in."""
from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.api import routes
from orchestrator.core.config import settings


async def test_metrics_endpoint_serves_the_classic_text_format(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    app = FastAPI()
    routes.register_routers(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(settings.metrics_path)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert "# TYPE " in resp.text
