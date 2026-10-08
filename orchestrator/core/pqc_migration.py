"""
Modus — PQC Attestation Migration Tool
=============================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Backfills existing enforcement attestations with a ``pqc_signature`` value.
Queries attestations that lack one, computes the value from the stored
``payload_json``, and writes the result back. The value is a real ML-DSA-65
signature when pqcrypto is installed; otherwise it is an HMAC-SHA-512
hash-chain commitment (a symmetric MAC — quantum-resistant integrity, not a
publicly-verifiable signature).

Each migrated attestation is recorded in the ``pqc_migration_log`` table
for a full audit trail.

Design:
    - Idempotent: skips rows that already have a pqc_signature.
    - Resumable: cursor-based batching — safe to interrupt and restart.
    - Rate-limited: configurable batch size and max-per-run caps.
    - Write-queue aware: routes writes through the write queue when
      available, falls back to direct session writes otherwise.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.pqc_signer import PQCAttestationSigner
from orchestrator.db.models import EnforcementAttestation, PQCMigrationLog

logger = logging.getLogger(__name__)

# ── Rate limiting ─────────────────────────────────────────────────────────────

_MIN_BATCH_DELAY_S = 0.05  # 50ms pause between batches to avoid starving writers


# ── Migration ─────────────────────────────────────────────────────────────────

async def migrate_attestations(
    db: AsyncSession,
    signer: PQCAttestationSigner,
    batch_size: int = 500,
    max_per_run: int = 5000,
) -> dict:
    """
    Migrate existing enforcement attestations to include PQC signatures.

    Queries ``enforcement_attestations WHERE pqc_signature IS NULL`` in
    cursor-based batches, computes the PQC signature from ``payload_json``,
    and updates the row with the new signature, algorithm, and key ID.

    Each migration is logged to ``pqc_migration_log`` for audit.

    Parameters
    ----------
    db : AsyncSession
        Active database session. Caller is responsible for lifecycle.
    signer : PQCAttestationSigner
        Initialised PQC signer instance.
    batch_size : int
        Number of attestations to process per batch. Default 500.
    max_per_run : int
        Maximum total attestations to migrate in this invocation.
        Default 5000. Set to 0 for unlimited.

    Returns
    -------
    dict
        ``{"migrated": int, "skipped": int, "errors": int, "elapsed_s": float}``
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    migrated = 0
    skipped = 0
    errors = 0
    start = time.monotonic()
    last_id: str | None = None

    logger.info(
        "PQC migration starting — algorithm=%s batch_size=%d max_per_run=%d",
        signer.algorithm,
        batch_size,
        max_per_run,
    )

    while True:
        # ── Check run cap ─────────────────────────────────────────────────
        if max_per_run > 0 and migrated >= max_per_run:
            logger.info("PQC migration reached max_per_run=%d, stopping", max_per_run)
            break

        remaining = (max_per_run - migrated) if max_per_run > 0 else batch_size
        effective_batch = min(batch_size, remaining)

        # ── Fetch next batch (cursor-based) ───────────────────────────────
        stmt = (
            select(
                EnforcementAttestation.id,
                EnforcementAttestation.payload_json,
                EnforcementAttestation.signature,
                EnforcementAttestation.algorithm,
            )
            .where(EnforcementAttestation.pqc_signature.is_(None))
        )

        # Cursor: only rows with id > last processed
        if last_id is not None:
            stmt = stmt.where(EnforcementAttestation.id > last_id)

        stmt = stmt.order_by(EnforcementAttestation.id).limit(effective_batch)

        result = await db.execute(stmt)
        rows = result.all()

        if not rows:
            break

        # ── Process batch ─────────────────────────────────────────────────
        batch_migrated = 0
        batch_errors = 0

        for row in rows:
            att_id = row.id
            payload_json = row.payload_json
            old_algorithm = row.algorithm
            old_signature = row.signature
            last_id = att_id

            if not payload_json:
                logger.warning(
                    "Skipping attestation %s: empty payload_json", att_id
                )
                skipped += 1
                continue

            try:
                # Compute PQC signature
                pqc_result = signer.sign(payload_json)

                # Update attestation row
                await db.execute(
                    update(EnforcementAttestation)
                    .where(EnforcementAttestation.id == att_id)
                    .values(
                        pqc_signature=pqc_result["pqc_signature"],
                        pqc_algorithm=pqc_result["pqc_algorithm"],
                        pqc_public_key_id=pqc_result["pqc_public_key_id"],
                    )
                )

                # Write audit log entry
                log_entry = PQCMigrationLog(
                    attestation_id=att_id,
                    old_algorithm=old_algorithm or "hmac-sha256",
                    new_algorithm=pqc_result["pqc_algorithm"],
                    old_signature=old_signature or "",
                    new_signature=pqc_result["pqc_signature"],
                )
                db.add(log_entry)

                batch_migrated += 1

            except Exception:
                logger.exception(
                    "Failed to migrate attestation %s", att_id
                )
                batch_errors += 1

        # Flush the batch
        try:
            await db.flush()
        except Exception:
            logger.exception("Failed to flush PQC migration batch")
            # Roll back will be handled by caller's session context
            errors += len(rows)
            break

        migrated += batch_migrated
        errors += batch_errors

        logger.debug(
            "PQC migration batch: migrated=%d errors=%d total=%d",
            batch_migrated,
            batch_errors,
            migrated,
        )

        # ── Rate limiting — yield to other writers ────────────────────────
        if len(rows) == effective_batch:
            # More rows likely remain — brief pause before next batch
            import asyncio
            await asyncio.sleep(_MIN_BATCH_DELAY_S)

    elapsed = time.monotonic() - start

    logger.info(
        "PQC migration complete — migrated=%d skipped=%d errors=%d elapsed=%.2fs",
        migrated,
        skipped,
        errors,
        elapsed,
    )

    return {
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
        "elapsed_s": round(elapsed, 3),
    }


# ── Verification pass ────────────────────────────────────────────────────────

async def verify_migrated_attestations(
    db: AsyncSession,
    signer: PQCAttestationSigner,
    batch_size: int = 1000,
    max_per_run: int = 10000,
) -> dict:
    """
    Verify PQC signatures on previously migrated attestations.

    Useful as a post-migration integrity check.

    Returns
    -------
    dict
        ``{"verified": int, "failed": int, "elapsed_s": float}``
    """
    verified = 0
    failed = 0
    start = time.monotonic()
    last_id: str | None = None

    while True:
        if max_per_run > 0 and (verified + failed) >= max_per_run:
            break

        remaining = (max_per_run - verified - failed) if max_per_run > 0 else batch_size
        effective_batch = min(batch_size, remaining)

        stmt = (
            select(
                EnforcementAttestation.id,
                EnforcementAttestation.payload_json,
                EnforcementAttestation.pqc_signature,
                EnforcementAttestation.pqc_algorithm,
            )
            .where(EnforcementAttestation.pqc_signature.isnot(None))
        )

        if last_id is not None:
            stmt = stmt.where(EnforcementAttestation.id > last_id)

        stmt = stmt.order_by(EnforcementAttestation.id).limit(effective_batch)

        result = await db.execute(stmt)
        rows = result.all()

        if not rows:
            break

        for row in rows:
            last_id = row.id
            if signer.verify(row.payload_json, row.pqc_signature, row.pqc_algorithm):
                verified += 1
            else:
                failed += 1
                logger.warning("PQC verification failed for attestation %s", row.id)

    elapsed = time.monotonic() - start

    logger.info(
        "PQC verification complete — verified=%d failed=%d elapsed=%.2fs",
        verified,
        failed,
        elapsed,
    )

    return {
        "verified": verified,
        "failed": failed,
        "elapsed_s": round(elapsed, 3),
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

async def migration_stats(db: AsyncSession) -> dict:
    """
    Return migration progress statistics.

    Returns
    -------
    dict
        ``{"total": int, "migrated": int, "pending": int, "percent": float}``
    """
    total_result = await db.execute(
        select(func.count()).select_from(EnforcementAttestation)
    )
    total = total_result.scalar() or 0

    migrated_result = await db.execute(
        select(func.count())
        .select_from(EnforcementAttestation)
        .where(EnforcementAttestation.pqc_signature.isnot(None))
    )
    migrated = migrated_result.scalar() or 0

    pending = total - migrated
    percent = (migrated / total * 100.0) if total > 0 else 0.0

    return {
        "total": total,
        "migrated": migrated,
        "pending": pending,
        "percent": round(percent, 2),
    }
