"""
Router registration for the Modus Orchestrator.

Centralises all ``app.include_router(...)`` calls so that ``main.py`` stays
slim.  Router registration, dashboard serving, healthz,
and the Prometheus metrics endpoint all live here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from orchestrator.core.config import settings

# Routers
from orchestrator.api import (
    apps, health, ingest, teams, thresholds, alerts, dashboard_data,
    pricing, notifications, policies, topology, routing, users, roles,
    finance, otel_ingest, ci_integration, attribution, gateway,
    compliance, governance, trajectory, attestation,
    prover, pqc, zk_proofs, sentinel, evolution, neuromorphic,
    audit_log,
)
from orchestrator.api.neuro_assurance import neuro_assurance_router
from orchestrator.api.federation import federation_router
from orchestrator.api.federation_admin import federation_admin_router
from orchestrator.api.swarm_governance import swarm_router
from orchestrator.api.insights import insights_router, reports_router, admin_router
from orchestrator.api.evaluate import evaluate_router
from orchestrator.api.pqc_assessment import pqc_assessment_router
from orchestrator.api.diagnostics import diagnostics_router
from orchestrator.api.onboarding import router as onboarding_router


def register_routers(app: FastAPI) -> None:
    """Attach all API routers to *app*, respecting settings."""

    # Core routers
    app.include_router(health.router, tags=["health"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["ingest"])
    app.include_router(apps.router, prefix="/api/v1", tags=["apps"])
    app.include_router(teams.router, prefix="/api/v1", tags=["teams"])
    app.include_router(thresholds.router, prefix="/api/v1", tags=["thresholds"])
    app.include_router(alerts.router, prefix="/api/v1", tags=["alerts"])
    app.include_router(pricing.router, prefix="/api/v1", tags=["pricing"])
    app.include_router(dashboard_data.router, prefix="/api/v1", tags=["dashboard"])
    app.include_router(otel_ingest.router, prefix="/api/v1", tags=["otel"])
    app.include_router(compliance.router, prefix="/api/v1", tags=["compliance"])
    app.include_router(audit_log.router, prefix="/api/v1", tags=["audit-log"])

    # Diagnostics
    app.include_router(diagnostics_router, prefix="/api/v1", tags=["diagnostics"])

    # Onboarding wizard
    app.include_router(onboarding_router, prefix="/api/v1", tags=["onboarding"])

    # User management & RBAC
    app.include_router(
        users.router, prefix="/api/v1", tags=["users"],
    )
    app.include_router(
        roles.router, prefix="/api/v1", tags=["roles"],
    )

    # Policy, enforcement, insights and operations
    app.include_router(
        policies.router, prefix="/api/v1", tags=["policy"],
    )
    app.include_router(
        evaluate_router, prefix="/api/v1", tags=["enforcement"],
    )
    app.include_router(
        insights_router, prefix="/api/v1", tags=["insights"],
    )
    app.include_router(
        reports_router, prefix="/api/v1", tags=["reports"],
    )
    app.include_router(
        admin_router, prefix="/api/v1", tags=["admin"],
    )
    app.include_router(
        notifications.router, prefix="/api/v1", tags=["notifications"],
    )
    app.include_router(
        topology.router, prefix="/api/v1", tags=["topology"],
    )

    # Routing engine
    app.include_router(
        routing.router, prefix="/api/v1", tags=["routing"],
    )

    # CI/CD integration
    app.include_router(
        ci_integration.router, prefix="/api/v1", tags=["ci-integration"],
    )

    # Attribution
    app.include_router(
        attribution.router, prefix="/api/v1", tags=["attribution"],
    )

    # Finance Intelligence
    app.include_router(
        finance.router, prefix="/api/v1", tags=["finance"],
    )

    # Autonomous Governance
    app.include_router(
        governance.governance_router, prefix="/api/v1", tags=["governance"],
    )

    # CoT Governance Ledger — governance
    from orchestrator.api.cot_ledger import router as cot_ledger_router
    app.include_router(
        cot_ledger_router, prefix="/api/v1", tags=["governance"],
    )

    # Global LLM Assistant
    from orchestrator.api.assistant import assistant_router
    app.include_router(
        assistant_router, prefix="/api/v1", tags=["assistant"],
    )

    # Nomus Regulatory Engine
    from orchestrator.api.nomus import router as nomus_router
    app.include_router(
        nomus_router, prefix="/api/v1", tags=["nomus"],
    )

    # Nomus webhook receiver (HMAC-verified; inert unless a secret is configured)
    from orchestrator.api.webhooks import webhook_router
    app.include_router(
        webhook_router, prefix="/api/v1", tags=["nomus"],
    )

    # Nomus integration management — auth-protected
    from orchestrator.api.nomus_integration import nomus_integration_router
    app.include_router(
        nomus_integration_router, prefix="/api/v1", tags=["nomus"],
    )

    # External Connections Dashboard
    from orchestrator.api.connections import router as connections_router
    app.include_router(
        connections_router, prefix="/api/v1", tags=["connections"],
    )

    # Trajectory Engine
    app.include_router(
        trajectory.trajectory_router, prefix="/api/v1", tags=["trajectory"],
    )

    # Enforcement Attestation
    app.include_router(
        attestation.attestation_router, prefix="/api/v1", tags=["compliance"],
    )

    # Policy Prover
    app.include_router(
        prover.prover_router, prefix="/api/v1", tags=["prover"],
    )

    # ── Phase 8 routers ─────────────────────────────────────────────────────

    # Post-Quantum Attestation
    app.include_router(
        pqc.pqc_router, prefix="/api/v1", tags=["compliance"],
    )

    # Trajectory Compliance Receipts (tamper-evident hash chain, not ZK)
    app.include_router(
        zk_proofs.zk_proofs_router, prefix="/api/v1", tags=["compliance"],
    )

    # TRiSM Multi-Agent Sentinel
    app.include_router(
        sentinel.sentinel_router, prefix="/api/v1", tags=["sentinel"],
    )

    # Constitutional Evolution
    app.include_router(
        evolution.evolution_router, prefix="/api/v1", tags=["governance"],
    )

    # Neuromorphic Edge Enforcement (Research Demo, software-simulated)
    app.include_router(
        neuromorphic.neuromorphic_router, prefix="/api/v1", tags=["neuromorphic"],
    )

    # ── Phase 9 routers ─────────────────────────────────────────────────────
    # ZK-PoE Chained Audit Ledger — RETIRED. Superseded by the tamper-evident
    # audit chain + signed checkpoints + offline verifier (see audit_log /
    # audit_chain / audit_checkpoint). The PoE endpoints had a dead write path
    # (empty in production); the audit chain is the real, wired evidence system.

    # NeuroAI Assurance Co-Evolution
    app.include_router(
        neuro_assurance_router, prefix="/api/v1", tags=["neuromorphic"],
    )

    # Federated ZK-Constitutional Optimizer
    app.include_router(
        federation_router, prefix="/api/v1", tags=["governance"],
    )

    # Federation Peer Management
    app.include_router(
        federation_admin_router, prefix="/api/v1", tags=["federation"],
    )

    # Threshold Approval Swarm Governance (Preview, not privacy-preserving MPC)
    app.include_router(
        swarm_router, prefix="/api/v1", tags=["governance"],
    )

    # ── Phase 10 routers ─────────────────────────────────────────────────────
    # PQC Assessment + HNDL + Agent Identity
    app.include_router(
        pqc_assessment_router, prefix="/api/v1", tags=["compliance"],
    )

    # ── Identity provisioning ──────────────────────────────
    from orchestrator.api.scim import scim_router

    # SCIM 2.0 Provisioning
    app.include_router(
        scim_router, prefix="/api/v1", tags=["scim"],
    )

    # ── Phase 11 routers — Fragility Fixes ──────────────────────────────────
    from orchestrator.api.ebpf import ebpf_router
    from orchestrator.api.crdt import crdt_router

    # eBPF Advisory Budget Tracking (Preview; kernel enforcement experimental)
    app.include_router(
        ebpf_router, prefix="/api/v1", tags=["ebpf"],
    )

    # CRDT Local-First Sync
    app.include_router(
        crdt_router, prefix="/api/v1", tags=["crdt"],
    )

    # Stream Guillotine is built into the Gateway proxy (no separate router)

    # Gateway proxy — language-agnostic AI governance (Phase 6)
    if settings.gateway_enabled:
        app.include_router(gateway.router, tags=["gateway"])

    # ── Dashboard (built-in serving — nginx/CDN optional for scale) ──────────
    if not settings.disable_dashboard:
        _dashboard_dir = Path(__file__).resolve().parent.parent.parent / "dashboard"
        _dashboard_file = _dashboard_dir / "index.html"
        if _dashboard_file.exists():
            # In development: read from disk on every request (hot reload)
            # In production: cache in memory for performance
            _dashboard_html = _dashboard_file.read_text(encoding="utf-8")
            _is_dev = settings.environment != "production"

            @app.get("/", include_in_schema=False)
            async def serve_dashboard() -> HTMLResponse:
                html = _dashboard_file.read_text(encoding="utf-8") if _is_dev else _dashboard_html
                resp = HTMLResponse(html)
                if _is_dev:
                    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                return resp

            # Serve favicon
            _icon_file = _dashboard_dir / "icon.png"
            if _icon_file.exists():
                from fastapi.responses import FileResponse

                @app.get("/dashboard/icon.png", include_in_schema=False)
                async def serve_favicon():
                    return FileResponse(_icon_file, media_type="image/png")

            # Serve CSS/JS assets from dashboard directory
            from starlette.staticfiles import StaticFiles

            app.mount(
                "/dashboard",
                StaticFiles(directory=str(_dashboard_dir)),
                name="dashboard-static",
            )

    # healthz alias for k8s and stress tests
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    # ── Prometheus metrics endpoint ────────────────────────────────────────────
    if settings.metrics_enabled:
        @app.get(settings.metrics_path, include_in_schema=False)
        async def metrics() -> Response:
            return Response(
                content=generate_latest(),
                media_type=CONTENT_TYPE_LATEST,
            )
