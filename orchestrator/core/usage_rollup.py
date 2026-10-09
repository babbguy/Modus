"""
Modus — Usage Rollup (hourly + daily aggregates)
=======================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Single place that writes ``usage_aggregates``. Every other module reads.

Source of truth
---------------
* **Counted usage enters exactly once, at ingest.** The write-queue writer
  claims the batch id (``ingest_batches``) and, in the *same* transaction,
  adds the batch's usage to the ``hourly`` row of each record's UTC hour and
  to the ``daily`` row of its UTC day. A retried batch id is skipped, so the
  increments are idempotent per batch.
* ``hourly`` rows are the source of truth for usage inside the hourly
  retention window (``hourly_aggregate_retention_days``, default 7 days).
* ``daily`` rows are maintained at the same time, so ``daily == sum(hourly)``
  for every (app, team, provider, model, day) by construction, and they are
  the source of truth once the hourly rows are pruned.
* ``usage_records`` hold per-call detail (raw-format records, SDK traces,
  session/span metadata). They are never re-summed into aggregates: SDK
  traces are samples of calls already counted in the SDK's pre-aggregated
  upload, and raw-format records are counted when they are ingested.

``reconcile_daily`` is the periodic safety net run by the aggregation task.
It recomputes the daily rows of the given days *from the hourly rows* and
REPLACES them when they differ, so running it any number of times never
changes a correct total.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import delete, func, select, text, update

from orchestrator.db.models import UsageAggregate

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_Q8 = Decimal("0.00000001")

# Columns whose values are summed when increments are merged into a row.
_SUM_INT = ("call_count", "input_tokens", "output_tokens", "total_tokens", "duration_ms_sum")
_SUM_DEC = ("input_cost", "output_cost", "total_cost")
_COMPARE = _SUM_INT + _SUM_DEC


# ── Time helpers ──────────────────────────────────────────────────────────────

def as_utc(ts: datetime) -> datetime:
    """Return ``ts`` as an aware UTC datetime (naive values are taken as UTC)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def hour_floor(ts: datetime) -> datetime:
    return as_utc(ts).replace(minute=0, second=0, microsecond=0)


def day_floor(ts: datetime) -> datetime:
    return as_utc(ts).replace(hour=0, minute=0, second=0, microsecond=0)


def _dec(v) -> Decimal:
    if v is None:
        return _ZERO
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


# ── Increments ────────────────────────────────────────────────────────────────

def increment_from_record(rec: dict) -> dict:
    """Build a usage increment (one call) from a validated usage-record dict."""
    in_tok = rec.get("input_tokens") or 0
    out_tok = rec.get("output_tokens") or 0
    total_tok = rec.get("total_tokens")
    if total_tok is None:
        total_tok = in_tok + out_tok
    dur = rec.get("duration_ms")
    return {
        "app_id": str(rec["app_id"]),
        "team_id": str(rec["team_id"]),
        "provider": rec["provider"],
        "model": rec.get("model"),
        "resource_type": rec["resource_type"],
        "hour_start": hour_floor(rec["timestamp"]),
        "call_count": 1,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total_tok,
        "input_cost": _dec(rec.get("input_cost")),
        "output_cost": _dec(rec.get("output_cost")),
        "total_cost": _dec(rec.get("total_cost")),
        "duration_ms_sum": dur or 0,
        "min_duration_ms": dur,
        "max_duration_ms": dur,
    }


def increments_from_records(records: Iterable[dict]) -> list[dict]:
    return [increment_from_record(r) for r in records]


def _fold(acc: dict, inc: dict) -> None:
    for col in _SUM_INT:
        acc[col] = (acc.get(col) or 0) + (inc.get(col) or 0)
    for col in _SUM_DEC:
        acc[col] = _dec(acc.get(col)) + _dec(inc.get(col))
    for col, pick in (("min_duration_ms", min), ("max_duration_ms", max)):
        new = inc.get(col)
        if new is not None:
            old = acc.get(col)
            acc[col] = new if old is None else pick(old, new)


def merge_increments(increments: Iterable[dict], granularity: str) -> dict[tuple, dict]:
    """Group increments by aggregate key for one granularity.

    The key mirrors the ``uq_aggregates_key`` constraint (resource_type is
    not part of it, so the first resource_type seen for a key is kept).
    """
    floor = hour_floor if granularity == "hourly" else day_floor
    out: dict[tuple, dict] = {}
    for inc in increments:
        period_start = floor(inc["hour_start"])
        key = (inc["app_id"], inc["team_id"], inc["provider"], inc.get("model"), period_start)
        acc = out.get(key)
        if acc is None:
            acc = {"resource_type": inc["resource_type"]}
            out[key] = acc
        _fold(acc, inc)
    return out


def increment_totals(increments: Iterable[dict]) -> dict:
    """Sum of a set of increments (used for the real-time spend counters)."""
    acc: dict = {}
    for inc in increments:
        _fold(acc, inc)
    return {
        "total_cost": _dec(acc.get("total_cost")),
        "call_count": acc.get("call_count") or 0,
        "input_tokens": acc.get("input_tokens") or 0,
        "output_tokens": acc.get("output_tokens") or 0,
        "duration_ms_sum": acc.get("duration_ms_sum") or 0,
    }


# ── Writes ────────────────────────────────────────────────────────────────────

def _dialect_insert(db):
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        return insert, func.least, func.greatest
    from sqlalchemy.dialects.sqlite import insert
    # Two-argument min/max are scalar functions on SQLite.
    return insert, func.min, func.max


def _period_end(granularity: str, start: datetime) -> datetime:
    return start + (timedelta(hours=1) if granularity == "hourly" else timedelta(days=1))


async def _add_to_row(db, granularity: str, key: tuple, acc: dict, source: str) -> None:
    """Additively merge one merged increment into its aggregate row."""
    app_id, team_id, provider, model, period_start = key
    calls = acc.get("call_count") or 0
    dur_sum = acc.get("duration_ms_sum") or 0
    now = datetime.now(timezone.utc)

    if model is None:
        # NULLs never collide in a UNIQUE constraint, so ON CONFLICT cannot
        # target a NULL-model row. Update exactly one existing row (by id) or
        # insert a new one. If two rows ever exist for the key, sums remain
        # exact because readers SUM across rows.
        existing_id = (await db.execute(
            select(UsageAggregate.id).where(
                UsageAggregate.app_id == app_id,
                UsageAggregate.team_id == team_id,
                UsageAggregate.provider == provider,
                UsageAggregate.model.is_(None),
                UsageAggregate.granularity == granularity,
                UsageAggregate.period_start == period_start,
            ).limit(1)
        )).scalar_one_or_none()
        if existing_id is not None:
            values: dict = {"computed_at": now}
            for col in _SUM_INT:
                values[col] = func.coalesce(getattr(UsageAggregate, col), 0) + (acc.get(col) or 0)
            for col in _SUM_DEC:
                values[col] = func.coalesce(getattr(UsageAggregate, col), 0) + _dec(acc.get(col))
            values["avg_duration_ms"] = (
                (func.coalesce(UsageAggregate.duration_ms_sum, 0) + dur_sum)
                / func.nullif(UsageAggregate.call_count + calls, 0)
            )
            _, least, greatest = _dialect_insert(db)
            if acc.get("min_duration_ms") is not None:
                new_min = acc["min_duration_ms"]
                values["min_duration_ms"] = least(
                    func.coalesce(UsageAggregate.min_duration_ms, new_min), new_min)
            if acc.get("max_duration_ms") is not None:
                new_max = acc["max_duration_ms"]
                values["max_duration_ms"] = greatest(
                    func.coalesce(UsageAggregate.max_duration_ms, new_max), new_max)
            await db.execute(
                update(UsageAggregate).where(UsageAggregate.id == existing_id).values(**values)
            )
            return

    insert, least, greatest = _dialect_insert(db)
    row = {
        "id": str(uuid.uuid4()),
        "app_id": app_id,
        "team_id": team_id,
        "provider": provider,
        "model": model,
        "resource_type": acc["resource_type"],
        "granularity": granularity,
        "period_start": period_start,
        "period_end": _period_end(granularity, period_start),
        "avg_duration_ms": (dur_sum // calls) if calls and dur_sum else None,
        "min_duration_ms": acc.get("min_duration_ms"),
        "max_duration_ms": acc.get("max_duration_ms"),
        "source": source,
        "computed_at": now,
    }
    for col in _SUM_INT:
        row[col] = acc.get(col) or 0
    for col in _SUM_DEC:
        row[col] = _dec(acc.get(col))

    stmt = insert(UsageAggregate).values(**row)
    if model is None:
        await db.execute(stmt)
        return

    update_set: dict = {"computed_at": now}
    for col in _SUM_INT + _SUM_DEC:
        update_set[col] = func.coalesce(getattr(UsageAggregate, col), 0) + stmt.excluded[col]
    update_set["avg_duration_ms"] = (
        (func.coalesce(UsageAggregate.duration_ms_sum, 0) + stmt.excluded.duration_ms_sum)
        / func.nullif(UsageAggregate.call_count + stmt.excluded.call_count, 0)
    )
    if row["min_duration_ms"] is not None:
        new_min = stmt.excluded.min_duration_ms
        update_set["min_duration_ms"] = least(
            func.coalesce(UsageAggregate.min_duration_ms, new_min), new_min)
    if row["max_duration_ms"] is not None:
        new_max = stmt.excluded.max_duration_ms
        update_set["max_duration_ms"] = greatest(
            func.coalesce(UsageAggregate.max_duration_ms, new_max), new_max)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=["app_id", "team_id", "provider", "model", "period_start", "granularity"],
        set_=update_set,
    ))


async def apply_increments(db, increments: list[dict], source: str) -> None:
    """Add usage increments to the hourly AND daily aggregate rows.

    Must run in the same transaction that claimed the batch id, so a batch is
    either fully counted once or not at all.
    """
    if not increments:
        return
    for granularity in ("hourly", "daily"):
        for key, acc in merge_increments(increments, granularity).items():
            await _add_to_row(db, granularity, key, acc, source)


# ── Reconciliation (daily := sum(hourly), REPLACE) ────────────────────────────

@dataclass
class ReconcileResult:
    days: int = 0
    keys_checked: int = 0
    keys_repaired: int = 0
    hourly_rows_pruned: int = 0


def _row_key(r) -> tuple:
    return (str(r.app_id), str(r.team_id), r.provider, r.model, day_floor(r.period_start))


async def _sum_by_day(db, granularity: str, lo: datetime, hi: datetime) -> dict[tuple, dict]:
    rows = (await db.execute(
        select(
            UsageAggregate.app_id, UsageAggregate.team_id, UsageAggregate.provider,
            UsageAggregate.model, UsageAggregate.resource_type, UsageAggregate.period_start,
            *[getattr(UsageAggregate, c) for c in _COMPARE],
            UsageAggregate.min_duration_ms, UsageAggregate.max_duration_ms,
        ).where(
            UsageAggregate.granularity == granularity,
            UsageAggregate.period_start >= lo,
            UsageAggregate.period_start < hi,
        )
    )).all()
    out: dict[tuple, dict] = {}
    for r in rows:
        key = _row_key(r)
        acc = out.get(key)
        if acc is None:
            acc = {"resource_type": r.resource_type}
            out[key] = acc
        _fold(acc, {c: getattr(r, c) for c in _COMPARE + ("min_duration_ms", "max_duration_ms")})
    return out


def _same(a: dict, b: dict) -> bool:
    for col in _SUM_INT:
        if int(a.get(col) or 0) != int(b.get(col) or 0):
            return False
    for col in _SUM_DEC:
        if _dec(a.get(col)).quantize(_Q8) != _dec(b.get(col)).quantize(_Q8):
            return False
    return True


def _mismatched(hourly: dict, daily: dict, *, fill_missing_only: bool) -> list[tuple]:
    if fill_missing_only:
        return [k for k in hourly if k not in daily]
    return [k for k, v in hourly.items() if k not in daily or not _same(v, daily[k])]


async def reconcile_daily(
    db, day_starts: Iterable[datetime], *, prune_hourly: bool = False,
) -> ReconcileResult:
    """Make each day's daily rows equal the sum of its hourly rows.

    For every (app, team, provider, model) that has hourly rows on a given
    day, the daily rows are REPLACED with the recomputed sum when they
    differ. Keys without hourly rows are left untouched (their hourly rows
    were already pruned, so the daily row is the source of truth).

    Only call it for days whose hourly rows are complete (inside the hourly
    retention window): replacing a daily row from a partially pruned day
    would lose usage.

    With ``prune_hourly=True`` (retention pruning of days that fell out of
    the window) existing daily rows are never replaced, because a late
    increment may have created a partial hourly row for an already-pruned
    day. Only keys that have hourly rows but no daily row at all are filled
    in, then the day's hourly rows are deleted. Nothing is lost: every
    increment updates the hourly AND the daily row in one transaction.

    Runs inside the caller's transaction. On PostgreSQL a repair first takes
    a SHARE ROW EXCLUSIVE lock on usage_aggregates so a concurrent ingest in
    another worker cannot slip an increment between the recompute and the
    replace (the lock is released at commit).
    """
    result = ReconcileResult()
    days = sorted({day_floor(d) for d in day_starts})
    is_pg = db.bind is not None and db.bind.dialect.name == "postgresql"
    for day in days:
        result.days += 1
        lo, hi = day, day + timedelta(days=1)
        hourly = await _sum_by_day(db, "hourly", lo, hi)
        if not hourly:
            continue
        daily = await _sum_by_day(db, "daily", lo, hi)
        mismatched = _mismatched(hourly, daily, fill_missing_only=prune_hourly)
        result.keys_checked += len(hourly)

        if mismatched and is_pg:
            await db.execute(text("LOCK TABLE usage_aggregates IN SHARE ROW EXCLUSIVE MODE"))
            # Re-read under the lock: values may have moved since the
            # optimistic comparison above.
            hourly = await _sum_by_day(db, "hourly", lo, hi)
            daily = await _sum_by_day(db, "daily", lo, hi)
            mismatched = _mismatched(hourly, daily, fill_missing_only=prune_hourly)
        # Pruning needs no lock: every increment updates the hourly AND the
        # daily row in one transaction, so the daily row already contains
        # anything that lands in an hourly row while we prune.

        for key in mismatched:
            app_id, team_id, provider, model, day_start = key
            acc = hourly[key]
            conds = [
                UsageAggregate.app_id == app_id,
                UsageAggregate.team_id == team_id,
                UsageAggregate.provider == provider,
                UsageAggregate.granularity == "daily",
                UsageAggregate.period_start == day_start,
                UsageAggregate.model.is_(None) if model is None else UsageAggregate.model == model,
            ]
            await db.execute(delete(UsageAggregate).where(*conds))
            calls = acc.get("call_count") or 0
            dur_sum = acc.get("duration_ms_sum") or 0
            row = {
                "id": str(uuid.uuid4()),
                "app_id": app_id, "team_id": team_id, "provider": provider, "model": model,
                "resource_type": acc["resource_type"], "granularity": "daily",
                "period_start": day_start, "period_end": day_start + timedelta(days=1),
                "avg_duration_ms": (dur_sum // calls) if calls and dur_sum else None,
                "min_duration_ms": acc.get("min_duration_ms"),
                "max_duration_ms": acc.get("max_duration_ms"),
                "source": "rollup",
                "computed_at": datetime.now(timezone.utc),
            }
            for col in _SUM_INT:
                row[col] = acc.get(col) or 0
            for col in _SUM_DEC:
                row[col] = _dec(acc.get(col))
            db.add(UsageAggregate(**row))
            result.keys_repaired += 1
        if mismatched:
            await db.flush()
            logger.warning(
                "Daily aggregates did not match hourly rows and were recomputed",
                extra={"day": day.date().isoformat(), "keys_repaired": len(mismatched)},
            )

        if prune_hourly:
            res = await db.execute(delete(UsageAggregate).where(
                UsageAggregate.granularity == "hourly",
                UsageAggregate.period_start >= lo,
                UsageAggregate.period_start < hi,
            ))
            result.hourly_rows_pruned += res.rowcount or 0
    return result


async def oldest_hourly_day(db, before: datetime) -> Optional[datetime]:
    """Return the earliest UTC day that still has hourly rows before ``before``."""
    first = (await db.execute(
        select(func.min(UsageAggregate.period_start)).where(
            UsageAggregate.granularity == "hourly",
            UsageAggregate.period_start < before,
        )
    )).scalar()
    if first is None:
        return None
    if isinstance(first, str):
        first = datetime.fromisoformat(first)
    return day_floor(first)
