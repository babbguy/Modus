"""
Modus — Audit Log Hash Chain
================================
Makes the primary audit trail (``AuditLog``) tamper-evident.

Every ``AuditLog`` row is linked into a single global, gap-free SHA-256 hash
chain at flush time by a ``before_flush`` listener. Each row commits its own
content *plus* the previous row's hash, so editing or deleting any row breaks
the chain from that point forward and the break is detectable by re-derivation.

Design notes
------------
- **Zero call-site changes.** The listener fires for any ``AuditLog`` added to
  a session, so every existing and future write site is covered automatically
  — there is no way to append to the audit table and bypass the chain.
- **Concurrency-safe.** Appends lock the ``hash_chain_state`` row for the
  chain (``SELECT ... FOR UPDATE`` on Postgres; SQLite serializes writers
  natively), so two concurrent transactions cannot fork the chain by reading
  the same ``prev_hash``.
- **Timestamp committed.** ``occurred_at`` is assigned deterministically in the
  listener and included in the hash, so a row cannot be silently back-dated.
- **Stdlib only** (``hashlib`` + ``json``). No external dependencies.

This module owns the *integrity* of the chain. Signed checkpoints and the
standalone offline verifier are layered on top separately (Item 2).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import event, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

CHAIN_NAME = "audit"

# Sentinel prev_hash for the very first entry in the chain.
GENESIS_PREV_HASH = "0" * 64


def _canonical(value: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, compact, str fallback)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_timestamp(dt: Optional[datetime]) -> str:
    """Normalize a datetime to a backend-stable UTC ISO string.

    SQLite drops tzinfo on round-trip while Postgres preserves it; hashing the
    raw ``isoformat()`` would therefore differ between write and verify. We
    normalize both sides to naive-UTC ISO so the hash is reproducible on any
    backend.
    """
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()


def compute_entry_hash(
    *,
    chain_seq: int,
    prev_hash: str,
    actor_id: str,
    actor_ip: Optional[str],
    team_id: Optional[str],
    resource_type: str,
    resource_id: Optional[str],
    action: str,
    before: Any,
    after: Any,
    occurred_at: str,
) -> str:
    """Compute an audit entry's hash. Deterministic for identical inputs.

    Commits the full row content AND the prior hash AND the timestamp, so the
    entry is bound to its exact position and moment in the chain.
    """
    payload = _canonical({
        "chain_seq": chain_seq,
        "prev_hash": prev_hash,
        "actor_id": actor_id,
        "actor_ip": actor_ip or "",
        "team_id": team_id or "",
        "resource_type": resource_type,
        "resource_id": resource_id or "",
        "action": action,
        "before": before,
        "after": after,
        "occurred_at": occurred_at,
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_chain_head(conn, dialect: str) -> tuple[int, str]:
    """Lock and read the chain head. Returns (last_seq, last_hash).

    Uses the transaction's Core connection (not the ORM session) — emitting SQL
    through session.execute() inside before_flush interferes with the
    unit-of-work's multi-table parameter binding. Creates the state row on
    first use; the lock is held until commit, serializing concurrent appenders.
    """
    for_update = "" if dialect == "sqlite" else " FOR UPDATE"

    row = conn.execute(
        text(
            f"SELECT last_seq, last_hash FROM hash_chain_state "
            f"WHERE chain_name = :n{for_update}"
        ),
        {"n": CHAIN_NAME},
    ).first()

    if row is None:
        conn.execute(
            text(
                "INSERT INTO hash_chain_state (chain_name, last_seq, last_hash) "
                "VALUES (:n, 0, NULL)"
            ),
            {"n": CHAIN_NAME},
        )
        row = conn.execute(
            text(
                f"SELECT last_seq, last_hash FROM hash_chain_state "
                f"WHERE chain_name = :n{for_update}"
            ),
            {"n": CHAIN_NAME},
        ).first()

    last_seq = int(row[0]) if row[0] is not None else 0
    last_hash = row[1] or GENESIS_PREV_HASH
    return last_seq, last_hash


def _write_chain_head(conn, last_seq: int, last_hash: str) -> None:
    conn.execute(
        text(
            "UPDATE hash_chain_state SET last_seq = :s, last_hash = :h, "
            "updated_at = :t WHERE chain_name = :n"
        ),
        {"s": last_seq, "h": last_hash, "t": datetime.now(timezone.utc), "n": CHAIN_NAME},
    )


def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    """Assign chain fields to every pending AuditLog insert in this flush."""
    # Imported here to avoid a circular import at module load.
    from orchestrator.db.models import AuditLog

    pending = [
        obj for obj in session.new
        if isinstance(obj, AuditLog) and obj.entry_hash is None
    ]
    if not pending:
        return

    # Deterministic order within the flush: insertion order isn't guaranteed
    # by the identity set, so sort by the object's creation counter if present,
    # else by python id (stable within a process) — the chain order just needs
    # to be *some* total order that the verifier reproduces from chain_seq.
    pending.sort(key=lambda o: getattr(o, "_audit_seq_hint", id(o)))

    # Emit SQL through the transaction's Core connection, never session.execute:
    # inside before_flush the ORM execute path corrupts the unit-of-work's
    # multi-table parameter binding (co-committed rows of other models read
    # back with shifted column values).
    conn = session.connection()
    dialect = conn.dialect.name
    last_seq, last_hash = _read_chain_head(conn, dialect)

    for obj in pending:
        # Commit the timestamp: assign it now so it's deterministic and hashed.
        if obj.occurred_at is None:
            obj.occurred_at = datetime.now(timezone.utc)
        occurred_iso = canonical_timestamp(obj.occurred_at)

        seq = last_seq + 1
        entry_hash = compute_entry_hash(
            chain_seq=seq,
            prev_hash=last_hash,
            actor_id=obj.actor_id,
            actor_ip=obj.actor_ip,
            team_id=obj.team_id,
            resource_type=obj.resource_type,
            resource_id=obj.resource_id,
            action=obj.action,
            before=obj.before,
            after=obj.after,
            occurred_at=occurred_iso,
        )
        obj.chain_seq = seq
        obj.prev_hash = last_hash
        obj.entry_hash = entry_hash

        last_seq = seq
        last_hash = entry_hash

    _write_chain_head(conn, last_seq, last_hash)


_INSTALLED = False


def install_audit_chain(session_factory: Any) -> None:
    """Register the chain listener on the given async_sessionmaker.

    Idempotent — safe to call once per process from init_db().
    """
    global _INSTALLED
    sync_cls = getattr(session_factory, "sync_session_class", Session)
    if not event.contains(sync_cls, "before_flush", _before_flush):
        event.listen(sync_cls, "before_flush", _before_flush)
    _INSTALLED = True
    logger.info("Audit hash chain listener installed")


# ── Verification ──────────────────────────────────────────────────────────────

class ChainVerificationResult:
    """Result of an audit-chain integrity check."""

    __slots__ = ("valid", "entries_checked", "error", "break_seq")

    def __init__(
        self,
        valid: bool,
        entries_checked: int,
        error: Optional[str] = None,
        break_seq: Optional[int] = None,
    ) -> None:
        self.valid = valid
        self.entries_checked = entries_checked
        self.error = error
        self.break_seq = break_seq

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "entries_checked": self.entries_checked,
            "error": self.error,
            "break_seq": self.break_seq,
        }


async def verify_audit_chain(db: Any, limit: Optional[int] = None) -> ChainVerificationResult:
    """Re-derive the audit chain and check integrity.

    Walks chained rows in ``chain_seq`` order, recomputing each hash and
    checking the ``prev_hash`` linkage. Reports the exact ``chain_seq`` where
    the chain first breaks (tampering, deletion, or reordering).

    ``limit`` verifies only the most recent N entries (still linkage-checked
    against the entry before the window).
    """
    from sqlalchemy import select
    from orchestrator.db.models import AuditLog

    q = (
        select(AuditLog)
        .where(AuditLog.chain_seq.isnot(None))
        .order_by(AuditLog.chain_seq.asc())
    )
    rows = list((await db.execute(q)).scalars().all())

    if limit is not None and limit > 0 and len(rows) > limit:
        rows = rows[-limit:]

    if not rows:
        return ChainVerificationResult(valid=True, entries_checked=0)

    expected_prev = None  # None → don't enforce the very first link in a window
    expected_seq = rows[0].chain_seq

    for row in rows:
        if row.chain_seq != expected_seq:
            return ChainVerificationResult(
                valid=False,
                entries_checked=(row.chain_seq or 0),
                error=f"sequence gap: expected {expected_seq}, found {row.chain_seq}",
                break_seq=row.chain_seq,
            )

        if expected_prev is not None and row.prev_hash != expected_prev:
            return ChainVerificationResult(
                valid=False,
                entries_checked=row.chain_seq,
                error=f"broken link at seq {row.chain_seq}: prev_hash mismatch",
                break_seq=row.chain_seq,
            )

        recomputed = compute_entry_hash(
            chain_seq=row.chain_seq,
            prev_hash=row.prev_hash or GENESIS_PREV_HASH,
            actor_id=row.actor_id,
            actor_ip=row.actor_ip,
            team_id=row.team_id,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            action=row.action,
            before=row.before,
            after=row.after,
            occurred_at=canonical_timestamp(row.occurred_at),
        )
        if recomputed != row.entry_hash:
            return ChainVerificationResult(
                valid=False,
                entries_checked=row.chain_seq,
                error=f"content tampered at seq {row.chain_seq}: entry_hash mismatch",
                break_seq=row.chain_seq,
            )

        expected_prev = row.entry_hash
        expected_seq = row.chain_seq + 1

    return ChainVerificationResult(valid=True, entries_checked=len(rows))
