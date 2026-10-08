"""
Tamper-evident audit trail.

Proof criteria (from the compliance-evidence assessment):
  1. Continuity: N audit writes produce a continuous, verifiable chain.
  2. Tamper: mutating one row's content is detected at the exact chain_seq.
  3. Deletion/reorder: removing a row breaks the chain and is reported.
  4. Coverage: an enforcement deny lands in the chained trail.
  5. Restart continuity: the chain continues correctly across a new session
     (state lives in the DB, not process memory).
  6. Unit: compute_entry_hash is deterministic and position-binding.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from orchestrator.core import audit_chain as ac
from orchestrator.core.audit_chain import verify_audit_chain
from orchestrator.db.models import AuditLog


@pytest.fixture(autouse=True)
def _install_chain_listener():
    """Install the chain listener for every test in this module.

    Production installs it once per process in init_db(); the test fixtures
    build their own sessionmakers, so install here and remove on teardown to
    keep the rest of the suite unaffected.
    """
    installed_here = not event.contains(Session, "before_flush", ac._before_flush)
    if installed_here:
        event.listen(Session, "before_flush", ac._before_flush)
    yield
    if installed_here and event.contains(Session, "before_flush", ac._before_flush):
        event.remove(Session, "before_flush", ac._before_flush)


@pytest_asyncio.fixture
async def db_session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _audit(action: str, actor: str = "tester", **kw) -> AuditLog:
    return AuditLog(
        actor_id=actor,
        resource_type=kw.pop("resource_type", "app"),
        resource_id=kw.pop("resource_id", "app-1"),
        action=action,
        after=kw.pop("after", {"k": "v"}),
        **kw,
    )


# ── 6. Unit: hash determinism + position binding ──────────────────────────────

def test_compute_entry_hash_deterministic_and_position_bound():
    base = dict(
        chain_seq=5, prev_hash="a" * 64, actor_id="u", actor_ip=None,
        team_id=None, resource_type="app", resource_id="x", action="created",
        before=None, after={"n": 1}, occurred_at="2026-07-24T00:00:00+00:00",
    )
    h1 = ac.compute_entry_hash(**base)
    h2 = ac.compute_entry_hash(**base)
    assert h1 == h2, "hash must be deterministic"

    # Changing position (seq or prev_hash) changes the hash → position-binding
    assert ac.compute_entry_hash(**{**base, "chain_seq": 6}) != h1
    assert ac.compute_entry_hash(**{**base, "prev_hash": "b" * 64}) != h1
    # Changing content changes the hash → tamper-binding
    assert ac.compute_entry_hash(**{**base, "after": {"n": 2}}) != h1
    assert ac.compute_entry_hash(**{**base, "occurred_at": "2026-07-24T00:00:01+00:00"}) != h1


# ── 1. Continuity ─────────────────────────────────────────────────────────────

async def test_chain_is_continuous_and_verifies(db_session):
    for i in range(10):
        db_session.add(_audit(f"action_{i}"))
    await db_session.commit()

    rows = (await db_session.execute(
        select(AuditLog).order_by(AuditLog.chain_seq.asc())
    )).scalars().all()
    chained = [r for r in rows if r.chain_seq is not None]
    assert len(chained) >= 10

    seqs = [r.chain_seq for r in chained]
    assert seqs == sorted(seqs), "chain_seq must be monotonic"
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "no gaps"

    # Every row links to the prior row's hash
    for prev, cur in zip(chained, chained[1:]):
        assert cur.prev_hash == prev.entry_hash

    result = await verify_audit_chain(db_session)
    assert result.valid is True
    assert result.entries_checked >= 10


# ── 2. Tamper detection at exact seq ──────────────────────────────────────────

async def test_tamper_detected_at_exact_seq(db_session):
    for i in range(6):
        db_session.add(_audit(f"a{i}"))
    await db_session.commit()

    assert (await verify_audit_chain(db_session)).valid is True

    # Pick a middle row and mutate its stored content directly (simulating a
    # malicious UPDATE that recomputes nothing).
    rows = (await db_session.execute(
        select(AuditLog).order_by(AuditLog.chain_seq.asc())
    )).scalars().all()
    target = rows[len(rows) // 2]
    target_seq = target.chain_seq

    # Mutate by chain_seq (an integer) — the UUID id column stores in a format
    # that a raw string bind won't match.
    n = (await db_session.execute(
        text("UPDATE audit_log SET after = :a WHERE chain_seq = :s"),
        {"a": '{"tampered": true}', "s": target_seq},
    )).rowcount
    assert n == 1
    await db_session.commit()
    db_session.expire_all()

    result = await verify_audit_chain(db_session)
    assert result.valid is False
    assert result.break_seq == target_seq
    assert "tamper" in (result.error or "").lower()


# ── 3. Deletion breaks the chain ──────────────────────────────────────────────

async def test_deletion_breaks_chain(db_session):
    for i in range(6):
        db_session.add(_audit(f"d{i}"))
    await db_session.commit()

    rows = (await db_session.execute(
        select(AuditLog).order_by(AuditLog.chain_seq.asc())
    )).scalars().all()
    victim_seq = rows[2].chain_seq

    n = (await db_session.execute(
        text("DELETE FROM audit_log WHERE chain_seq = :s"), {"s": victim_seq}
    )).rowcount
    assert n == 1
    await db_session.commit()
    db_session.expire_all()

    result = await verify_audit_chain(db_session)
    assert result.valid is False
    # Deletion shows up as either a sequence gap or a broken link at the
    # following entry.
    assert result.break_seq is not None


# ── 4. Enforcement coverage ───────────────────────────────────────────────────

async def test_enforcement_deny_lands_in_chain(db_session):
    # Simulate what the write-queue drain does for a deny decision.
    db_session.add(AuditLog(
        actor_id="enforcement-engine",
        team_id=None,
        resource_type="app",
        resource_id="app-42",
        action="enforcement_deny",
        after={"decision": "deny", "reason": "budget exceeded"},
    ))
    await db_session.commit()

    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "enforcement_deny")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].entry_hash is not None, "enforcement action must be chained"

    assert (await verify_audit_chain(db_session)).valid is True


# ── 5. Restart continuity (state in DB, not memory) ───────────────────────────

async def test_chain_continues_across_sessions(db_session_factory):
    # First session appends 3 entries and commits.
    async with db_session_factory() as s1:
        for i in range(3):
            s1.add(_audit(f"s1_{i}"))
        await s1.commit()

    # A *new* session (as after a restart) must continue the same chain.
    async with db_session_factory() as s2:
        for i in range(3):
            s2.add(_audit(f"s2_{i}"))
        await s2.commit()

        result = await verify_audit_chain(s2)
        assert result.valid is True

        rows = (await s2.execute(
            select(AuditLog).order_by(AuditLog.chain_seq.asc())
        )).scalars().all()
        chained = [r for r in rows if r.chain_seq is not None]
        seqs = [r.chain_seq for r in chained]
        # Continuous across the session boundary — no reset to 1.
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        # The s2 rows chain onto the s1 rows.
        assert len(chained) >= 6


# ── 7. API surface ────────────────────────────────────────────────────────────

async def test_verify_endpoint_and_listing_expose_chain(client, db_session):
    # Empty chain verifies clean.
    resp = await client.get("/api/v1/audit-log/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["entries_checked"] == 0

    db_session.add(_audit("created"))
    db_session.add(_audit("updated"))
    await db_session.commit()

    resp = await client.get("/api/v1/audit-log/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["entries_checked"] == 2
    assert body["error"] is None

    listing = await client.get("/api/v1/audit-log")
    assert listing.status_code == 200
    entries = listing.json()
    assert len(entries) == 2
    assert all(e["entry_hash"] and e["chain_seq"] for e in entries)
