"""
Modus Federator — Application Entry Point
=================================================
FastAPI application for the blind relay service.

This service:
    - Accepts encrypted constitution deltas with ZK proofs
    - NEVER decrypts customer payloads
    - Aggregates metadata signals (fitness, gene_count, generation_span)
    - Serves merged collective intelligence to subscribers
    - Maintains a Merkle audit trail

Deployment: Single process, SQLite, $10-20/mo VPS.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from federator.config import settings
from federator.core.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# ── Rate limiters ────────────────────────────────────────────────────────────
submit_limiter = RateLimiter(
    rate_per_hour=settings.rate_limit_submits_per_hour,
    burst=settings.rate_limit_burst,
)
read_limiter = RateLimiter(
    rate_per_hour=settings.rate_limit_reads_per_hour,
    burst=settings.rate_limit_burst,
)

# ── Background tasks ────────────────────────────────────────────────────────
_aggregation_task: asyncio.Task | None = None


async def _periodic_aggregation() -> None:
    """Background loop: recompute merged results periodically."""
    interval = settings.aggregation_interval_minutes * 60
    while True:
        await asyncio.sleep(interval)
        try:
            from federator.db.session import async_session_factory
            from federator.core.blind_aggregator import run_aggregation
            async with async_session_factory() as db:
                results = await run_aggregation(
                    db,
                    min_contributors=settings.min_contributors_for_result,
                )
                await db.commit()
                if results:
                    logger.info("Periodic aggregation: %d results computed", len(results))
        except Exception:
            logger.exception("Periodic aggregation failed")


async def _periodic_merkle_anchor() -> None:
    """Background loop: snapshot Merkle roots for the audit trail."""
    while True:
        await asyncio.sleep(3600)  # hourly
        try:
            from federator.db.session import async_session_factory
            from federator.db.models import EncryptedDelta, MerkleAnchor
            from sqlalchemy import select

            async with async_session_factory() as db:
                now = datetime.now(timezone.utc)
                epoch_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"

                # Get all leaf hashes for this epoch
                result = await db.execute(
                    select(EncryptedDelta.merkle_leaf_hash)
                    .where(EncryptedDelta.epoch_week == epoch_week)
                    .order_by(EncryptedDelta.received_at)
                )
                leaves = [row[0] for row in result.all()]

                if not leaves:
                    continue

                # Build Merkle tree using stdlib
                layer = leaves[:]
                while len(layer) > 1:
                    if len(layer) % 2 == 1:
                        layer.append(layer[-1])
                    next_layer = []
                    for i in range(0, len(layer), 2):
                        combined = bytes.fromhex(layer[i]) + bytes.fromhex(layer[i + 1])
                        next_layer.append(hashlib.sha256(combined).hexdigest())
                    layer = next_layer

                root_hash = layer[0]

                # Check if we already have an anchor for this root
                existing = await db.execute(
                    select(MerkleAnchor)
                    .where(
                        MerkleAnchor.epoch_week == epoch_week,
                        MerkleAnchor.root_hash == root_hash,
                    )
                    .limit(1)
                )
                if existing.scalar_one_or_none():
                    continue

                anchor = MerkleAnchor(
                    root_hash=root_hash,
                    leaf_count=len(leaves),
                    epoch_week=epoch_week,
                )
                db.add(anchor)
                await db.commit()
                logger.info("Merkle anchor: root=%s leaves=%d week=%s", root_hash[:16], len(leaves), epoch_week)

        except Exception:
            logger.exception("Merkle anchor failed")


async def _periodic_cleanup() -> None:
    """Background loop: clean up stale rate limiter buckets."""
    while True:
        await asyncio.sleep(3600)
        removed = submit_limiter.cleanup() + read_limiter.cleanup()
        if removed:
            logger.debug("Rate limiter cleanup: removed %d stale buckets", removed)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Init DB
    from federator.db.session import init_db
    await init_db()

    # Inject rate limiters into API modules
    from federator.api.ingest import set_submit_limiter
    from federator.api.results import set_read_limiter
    set_submit_limiter(submit_limiter)
    set_read_limiter(read_limiter)

    # Start background tasks
    tasks = [
        asyncio.create_task(_periodic_aggregation()),
        asyncio.create_task(_periodic_merkle_anchor()),
        asyncio.create_task(_periodic_cleanup()),
    ]

    logger.info(
        "Modus Federator v%s started — %s mode, aggregation every %dm",
        settings.version, settings.environment, settings.aggregation_interval_minutes,
    )

    yield

    # Shutdown
    for t in tasks:
        t.cancel()
    logger.info("Modus Federator shutting down")


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Modus Federator",
    description=(
        "Blind relay for federated ZK-constitutional optimization. "
        "Accepts encrypted constitution deltas, aggregates metadata signals, "
        "serves collective intelligence. NEVER decrypts customer payloads."
    ),
    version=settings.version,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)

# CORS — use configured origins; never allow wildcard even in debug mode
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins if settings.cors_origins else [],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Mount routers ────────────────────────────────────────────────────────────
from federator.api.health import health_router
from federator.api.ingest import ingest_router
from federator.api.results import results_router
from federator.api.portal import portal_router
from federator.api.audit import audit_router

app.include_router(health_router)
app.include_router(ingest_router)
app.include_router(results_router)
app.include_router(portal_router)
app.include_router(audit_router)
