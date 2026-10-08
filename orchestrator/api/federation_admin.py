"""
Modus — Federation Peer Management API
==========================================
CRUD + connectivity testing for managed subsidiary Modus instances.

POST   /api/v1/admin/federation/peers              — add peer
GET    /api/v1/admin/federation/peers              — list peers
PATCH  /api/v1/admin/federation/peers/{id}         — update peer
POST   /api/v1/admin/federation/peers/{id}/pause   — pause peer
POST   /api/v1/admin/federation/peers/{id}/resume  — resume peer
DELETE /api/v1/admin/federation/peers/{id}         — delete peer
POST   /api/v1/admin/federation/peers/{id}/test    — test connectivity
GET    /api/v1/admin/federation/peers/{id}/history — sync history
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db.session import get_read_session, get_session

logger = logging.getLogger(__name__)

federation_admin_router = APIRouter(
    prefix="/admin/federation", tags=["federation"],
)


# ── Pydantic schemas ────────────────────────────────────────────────────────


class PeerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    peer_url: str = Field(..., min_length=1, max_length=512)
    api_key: str = Field(..., min_length=8)


class PeerUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=256)
    peer_url: Optional[str] = Field(None, min_length=1, max_length=512)
    api_key: Optional[str] = Field(None, min_length=8)


class PeerResponse(BaseModel):
    id: str
    team_id: Optional[str] = None
    name: str
    peer_url_masked: str
    api_key_prefix: str
    status: str
    last_heartbeat_at: Optional[str] = None
    last_heartbeat_latency_ms: Optional[int] = None
    last_sync_at: Optional[str] = None
    last_sync_status: Optional[str] = None
    last_error: Optional[str] = None
    metadata_json: Optional[dict] = None
    created_at: str
    updated_at: str


class PeerCreateResponse(PeerResponse):
    """Returned only on creation — includes the full API key once."""
    api_key: str


class PeerTestResponse(BaseModel):
    reachable: bool
    latency_ms: Optional[int] = None
    peer_version: Optional[str] = None
    error: Optional[str] = None


class SyncLogEntry(BaseModel):
    id: str
    direction: str
    status: str
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None
    synced_at: str


# ── Helpers ──────────────────────────────────────────────────────────────────


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def _mask_url(url: str) -> str:
    """Return scheme + host only (e.g. https://sub.example.com)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else url


def _dt_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _peer_to_response(peer: Any) -> PeerResponse:
    return PeerResponse(
        id=peer.id,
        team_id=peer.team_id,
        name=peer.name,
        peer_url_masked=_mask_url(peer.peer_url),
        api_key_prefix=peer.api_key_prefix,
        status=peer.status,
        last_heartbeat_at=_dt_iso(peer.last_heartbeat_at),
        last_heartbeat_latency_ms=peer.last_heartbeat_latency_ms,
        last_sync_at=_dt_iso(peer.last_sync_at),
        last_sync_status=peer.last_sync_status,
        last_error=peer.last_error,
        metadata_json=peer.metadata_json,
        created_at=_dt_iso(peer.created_at),  # type: ignore[arg-type]
        updated_at=_dt_iso(peer.updated_at),  # type: ignore[arg-type]
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@federation_admin_router.post("/peers", response_model=PeerCreateResponse)
async def add_peer(
    body: PeerCreateRequest,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    """Register a new subsidiary Modus peer."""
    from orchestrator.db.models import FederationPeer

    key_hash = _hash_key(body.api_key)
    key_prefix = body.api_key[:8]

    peer = FederationPeer(
        team_id=identity.team_id,
        name=body.name,
        peer_url=body.peer_url,
        api_key_hash=key_hash,
        api_key_prefix=key_prefix,
        status="active",
    )
    session.add(peer)
    await session.commit()
    await session.refresh(peer)

    resp = _peer_to_response(peer)
    return PeerCreateResponse(
        **resp.model_dump(),
        api_key=body.api_key,
    )


@federation_admin_router.get("/peers", response_model=list[PeerResponse])
async def list_peers(
    status: Optional[str] = None,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_read_session),
):
    """List all managed federation peers."""
    from orchestrator.db.models import FederationPeer

    stmt = select(FederationPeer).order_by(FederationPeer.created_at.desc())
    if status:
        stmt = stmt.where(FederationPeer.status == status)
    result = await session.execute(stmt)
    peers = result.scalars().all()
    return [_peer_to_response(p) for p in peers]


@federation_admin_router.patch("/peers/{peer_id}", response_model=PeerResponse)
async def update_peer(
    peer_id: str,
    body: PeerUpdateRequest,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    """Update a federation peer's name, URL, or API key."""
    from orchestrator.db.models import FederationPeer

    result = await session.execute(
        select(FederationPeer).where(FederationPeer.id == peer_id)
    )
    peer = result.scalar_one_or_none()
    if not peer:
        raise HTTPException(status_code=404, detail="Peer not found")

    if body.name is not None:
        peer.name = body.name
    if body.peer_url is not None:
        peer.peer_url = body.peer_url
    if body.api_key is not None:
        peer.api_key_hash = _hash_key(body.api_key)
        peer.api_key_prefix = body.api_key[:8]

    # SQLite onupdate doesn't fire — set explicitly
    peer.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(peer)
    return _peer_to_response(peer)


@federation_admin_router.post("/peers/{peer_id}/pause", response_model=PeerResponse)
async def pause_peer(
    peer_id: str,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    """Pause a federation peer — stops sync and heartbeat."""
    from orchestrator.db.models import FederationPeer

    result = await session.execute(
        select(FederationPeer).where(FederationPeer.id == peer_id)
    )
    peer = result.scalar_one_or_none()
    if not peer:
        raise HTTPException(status_code=404, detail="Peer not found")

    peer.status = "paused"
    peer.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(peer)
    return _peer_to_response(peer)


@federation_admin_router.post("/peers/{peer_id}/resume", response_model=PeerResponse)
async def resume_peer(
    peer_id: str,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    """Resume a paused federation peer."""
    from orchestrator.db.models import FederationPeer

    result = await session.execute(
        select(FederationPeer).where(FederationPeer.id == peer_id)
    )
    peer = result.scalar_one_or_none()
    if not peer:
        raise HTTPException(status_code=404, detail="Peer not found")

    peer.status = "active"
    peer.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(peer)
    return _peer_to_response(peer)


@federation_admin_router.delete("/peers/{peer_id}", status_code=204)
async def delete_peer(
    peer_id: str,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    """Remove a federation peer and all its sync history."""
    from orchestrator.db.models import FederationPeer

    result = await session.execute(
        select(FederationPeer).where(FederationPeer.id == peer_id)
    )
    peer = result.scalar_one_or_none()
    if not peer:
        raise HTTPException(status_code=404, detail="Peer not found")

    await session.delete(peer)
    await session.commit()


@federation_admin_router.post("/peers/{peer_id}/test", response_model=PeerTestResponse)
async def test_peer(
    peer_id: str,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    """Test connectivity to a federation peer via /healthz probe."""
    from orchestrator.db.models import FederationPeer, FederationPeerSyncLog

    result = await session.execute(
        select(FederationPeer).where(FederationPeer.id == peer_id)
    )
    peer = result.scalar_one_or_none()
    if not peer:
        raise HTTPException(status_code=404, detail="Peer not found")

    now = datetime.now(timezone.utc)
    reachable = False
    latency_ms: Optional[int] = None
    peer_version: Optional[str] = None
    error_msg: Optional[str] = None

    try:
        url = peer.peer_url.rstrip("/") + "/healthz"
        t0 = time.monotonic()

        def _probe() -> bytes:
            # Blocking urllib call — offloaded to a thread so the event loop is
            # never blocked for up to 5s (Law 4: non-blocking).
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read(4096)

        body = await asyncio.get_running_loop().run_in_executor(None, _probe)
        latency_ms = int((time.monotonic() - t0) * 1000)
        reachable = True
        # Try to extract version from JSON response
        try:
            import json
            data = json.loads(body)
            peer_version = data.get("version") or data.get("v")
        except (ValueError, AttributeError, UnicodeDecodeError) as exc:
            # Peer version is optional metadata; reachability already established.
            logger.debug("Federation peer %s: could not parse version: %s", peer.peer_url, exc)
    except urllib.error.URLError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000) if 't0' in dir() else None
        error_msg = str(exc.reason) if hasattr(exc, "reason") else str(exc)
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000) if 't0' in dir() else None
        error_msg = str(exc)

    # Record sync log entry
    log_entry = FederationPeerSyncLog(
        peer_id=peer.id,
        direction="heartbeat",
        status="success" if reachable else "error",
        latency_ms=latency_ms,
        error_message=error_msg,
    )
    session.add(log_entry)

    # Update peer heartbeat fields
    peer.last_heartbeat_at = now
    peer.last_heartbeat_latency_ms = latency_ms
    if not reachable:
        peer.status = "unreachable"
        peer.last_error = error_msg
    else:
        if peer.status == "unreachable":
            peer.status = "active"
        peer.last_error = None
    peer.updated_at = now

    await session.commit()

    return PeerTestResponse(
        reachable=reachable,
        latency_ms=latency_ms,
        peer_version=peer_version,
        error=error_msg,
    )


@federation_admin_router.get(
    "/peers/{peer_id}/history", response_model=list[SyncLogEntry],
)
async def peer_history(
    peer_id: str,
    limit: int = 50,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_read_session),
):
    """Get sync history for a federation peer."""
    from orchestrator.db.models import FederationPeer, FederationPeerSyncLog

    # Verify peer exists
    result = await session.execute(
        select(FederationPeer).where(FederationPeer.id == peer_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Peer not found")

    stmt = (
        select(FederationPeerSyncLog)
        .where(FederationPeerSyncLog.peer_id == peer_id)
        .order_by(FederationPeerSyncLog.synced_at.desc())
        .limit(min(limit, 200))
    )
    result = await session.execute(stmt)
    logs = result.scalars().all()

    return [
        SyncLogEntry(
            id=log.id,
            direction=log.direction,
            status=log.status,
            latency_ms=log.latency_ms,
            error_message=log.error_message,
            synced_at=_dt_iso(log.synced_at),  # type: ignore[arg-type]
        )
        for log in logs
    ]
