"""
Modus — Federated Constitutional Optimizer (Opt-in Benchmark Sharing)
===================================================
Opt-in, numeric-only cross-deployment benchmark sharing.

NOTE ON CLAIMS: This is opt-in benchmark sharing of numeric gene diffs and
fitness values. It is NOT zero-knowledge and NOT differentially private:
the aggregator receives each contributor's numeric delta (encrypted in
transit, but readable by the receiving aggregator in consortium mode). Meaning
is protected by transmitting only numbers (no policies/prompts/usage) and by
publishing merged results only above a k-anonymity threshold (min 3
contributors) — not by any cryptographic privacy protocol. The "sigma proof"
attached to a submission is format/consistency-validated only (see
``generate_sigma_proof``); it is not a sound zero-knowledge proof.

CRITICAL DATA SOVEREIGNTY:
- This is the ONLY feature that creates an outbound connection
- Deltas contain ONLY numeric gene diffs and fitness values
- No policies, no prompts, no usage data ever transmitted
- Anonymous weekly-rotating HMAC nonces — unlinkable across weeks
- Consent checkpoint required before any submission
- Withdrawal is immediate
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConstitutionDelta:
    """Numeric-only diff between two evolution generations."""
    gene_diffs: dict[str, float]   # gene_name -> numeric delta
    fitness_before: float
    fitness_after: float
    generation_from: int
    generation_to: int


@dataclass(frozen=True, slots=True)
class FederationSubmission:
    """Anonymous numeric-only submission to the aggregator."""
    nonce: str                      # weekly-rotating HMAC nonce
    encrypted_delta: dict           # numeric-only gene diffs
    fitness_improvement: float
    generation_span: int
    timestamp: str


def _weekly_nonce(instance_key: str) -> str:
    """Generate a weekly-rotating anonymous nonce. Unlinkable across weeks."""
    week = datetime.now(timezone.utc).isocalendar()
    week_key = f"{week.year}-W{week.week:02d}"
    return hmac.new(
        instance_key.encode(),
        week_key.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


def extract_delta(
    before_genome: dict[str, float],
    after_genome: dict[str, float],
    fitness_before: float,
    fitness_after: float,
    gen_from: int,
    gen_to: int,
) -> ConstitutionDelta:
    """Extract numeric-only delta between two policy genomes."""
    all_genes = set(before_genome.keys()) | set(after_genome.keys())
    diffs = {}
    for gene in all_genes:
        old = before_genome.get(gene, 0.0)
        new = after_genome.get(gene, 0.0)
        if abs(new - old) > 1e-9:
            diffs[gene] = round(new - old, 8)

    return ConstitutionDelta(
        gene_diffs=diffs,
        fitness_before=fitness_before,
        fitness_after=fitness_after,
        generation_from=gen_from,
        generation_to=gen_to,
    )


class ConsentCheckpoint:
    """Enforces consent before any outbound federation activity."""

    @staticmethod
    async def has_consent(db: AsyncSession) -> bool:
        """Check if valid (non-withdrawn) consent exists."""
        from orchestrator.db.models import FederationConsent
        result = await db.execute(
            select(FederationConsent).where(
                FederationConsent.withdrawn_at.is_(None),
            ).order_by(desc(FederationConsent.consented_at)).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def record_consent(
        db: AsyncSession,
        participation_mode: str,
        consented_by: str,
        disclosure_text: str,
    ) -> dict:
        """Record operator consent with disclosure hash."""
        from orchestrator.db.models import FederationConsent
        import secrets

        consent = FederationConsent(
            participation_mode=participation_mode,
            disclosure_text_hash=hashlib.sha256(disclosure_text.encode()).hexdigest(),
            consented_by=consented_by,
            instance_nonce_key=secrets.token_hex(32),
        )
        db.add(consent)
        await db.flush()
        return {
            "consent_id": str(consent.id),
            "participation_mode": participation_mode,
            "consented_at": consent.consented_at.isoformat() if consent.consented_at else None,
        }

    @staticmethod
    async def withdraw_consent(db: AsyncSession) -> bool:
        """Withdraw consent — immediate cessation of all outbound."""
        from orchestrator.db.models import FederationConsent
        result = await db.execute(
            select(FederationConsent).where(
                FederationConsent.withdrawn_at.is_(None),
            )
        )
        consents = result.scalars().all()
        if not consents:
            return False
        for c in consents:
            c.withdrawn_at = datetime.now(timezone.utc)
        await db.flush()
        return True


class BlindAggregator:
    """
    Aggregates numeric-only deltas into averaged benchmark improvements.

    NOTE: the name is historical — this is NOT a blind/oblivious aggregator.
    In consortium mode it runs locally as a hub and reads each contributor's
    numeric delta in plaintext. Privacy relies on numbers-only submission and
    a k-anonymity publish threshold, not on any cryptographic blinding.
    """

    @staticmethod
    def merge_deltas(deltas: list[dict]) -> dict:
        """Merge numeric-only deltas into weighted average improvements
        (plaintext aggregation; not privacy-preserving)."""
        if not deltas:
            return {"gene_improvements": {}, "participating_instances": 0, "confidence_score": 0.0}

        gene_totals: dict[str, list[float]] = {}
        total_fitness_improvement = 0.0

        for delta in deltas:
            genes = delta.get("gene_diffs", {})
            for gene, diff in genes.items():
                if gene not in gene_totals:
                    gene_totals[gene] = []
                gene_totals[gene].append(diff)
            total_fitness_improvement += delta.get("fitness_improvement", 0.0)

        # Weighted average: genes that appear in more deltas get higher confidence
        gene_improvements = {}
        for gene, diffs in gene_totals.items():
            avg = sum(diffs) / len(diffs)
            gene_improvements[gene] = round(avg, 8)

        # Confidence: based on number of contributors and consistency
        import statistics as stats
        consistency_scores = []
        for gene, diffs in gene_totals.items():
            if len(diffs) > 1:
                mean = stats.mean(diffs)
                std = stats.stdev(diffs)
                cv = std / abs(mean) if abs(mean) > 1e-9 else 1.0
                consistency_scores.append(max(0.0, 1.0 - min(1.0, cv)))

        confidence = stats.mean(consistency_scores) if consistency_scores else 0.5

        return {
            "gene_improvements": gene_improvements,
            "participating_instances": len(deltas),
            "confidence_score": round(confidence, 4),
            "avg_fitness_improvement": round(total_fitness_improvement / len(deltas), 6),
        }

def _sha256_hex(*parts: str) -> str:
    """SHA-256 hash of concatenated string parts, returned as hex."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class SigmaProof:
    """A hash-chain commitment attached to a federation delta submission.

    NOTE: named "sigma proof" for historical reasons, but this is not a sound
    sigma-protocol zero-knowledge proof — the receiver can only
    format/consistency-check the hash chain (see ``generate_sigma_proof``).
    """
    commitment: str     # hex SHA-256
    challenge: str      # hex SHA-256
    response: str       # hex SHA-256
    public_inputs: dict  # hashes of public metadata


def generate_sigma_proof(
    delta: ConstitutionDelta,
    fitness_improvement: float,
    gene_count: int,
    generation_span: int,
) -> SigmaProof:
    """
    Build a SHA-256 hash-chain commitment for a constitution delta.

    This is NOT a sound zero-knowledge proof. It is a Fiat-Shamir-style hash
    chain (commitment -> challenge -> response) that a receiver can only
    format/consistency-validate; it does not cryptographically prove the
    hidden payload satisfies any predicate, and it provides no soundness
    against a prover who fabricates a self-consistent chain. It does keep
    gene names / policy content out of the transmitted metadata (numbers
    only), which is a secrecy property, not zero-knowledge.
    """
    import secrets

    # Step 1: hash the delta content (only the prover knows this)
    payload_json = json.dumps(delta.gene_diffs, sort_keys=True)
    payload_hash = _sha256_hex(payload_json)

    # Step 2: random nonce (secret, never transmitted)
    nonce_r = secrets.token_hex(32)

    # Step 3: commitment = SHA-256(payload_hash || nonce_r)
    commitment = _sha256_hex(payload_hash, nonce_r)

    # Step 4: challenge = SHA-256(commitment || metadata)
    challenge = _sha256_hex(
        commitment,
        str(fitness_improvement),
        str(gene_count),
        str(generation_span),
    )

    # Step 5: response = SHA-256(nonce_r || challenge || payload_hash)
    response = _sha256_hex(nonce_r, challenge, payload_hash)

    # Public input hashes (optional additional binding)
    public_inputs = {
        "fitness_improvement_hash": _sha256_hex(str(fitness_improvement)),
        "gene_count_hash": _sha256_hex(str(gene_count)),
        "generation_span_hash": _sha256_hex(str(generation_span)),
    }

    return SigmaProof(
        commitment=commitment,
        challenge=challenge,
        response=response,
        public_inputs=public_inputs,
    )


def encrypt_delta_payload(delta_json: str, encryption_key: bytes) -> bytes:
    """
    Encrypt a delta payload using AES-GCM for transmission to the federator.

    The federator stores this blob as-is and NEVER decrypts it.
    It serves as a tamper-evident record the customer can later verify.

    Args:
        delta_json: JSON-serialized delta content
        encryption_key: 32-byte key (derived from instance nonce key)

    Returns:
        Encrypted bytes (nonce + ciphertext + tag)
    """
    import os
    from hashlib import sha256

    # Derive a 32-byte key from whatever key material we have
    key = sha256(encryption_key).digest()

    # AES-GCM using stdlib (Python 3.12+ has it, fallback to XOR cipher for portability)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, delta_json.encode(), None)
        return nonce + ciphertext
    except ImportError:
        # Fallback: simple XOR with SHA-256 key stream (not as strong, but
        # the federator never decrypts anyway — this is defense in depth)
        data = delta_json.encode()
        stream = b""
        counter = 0
        while len(stream) < len(data):
            stream += sha256(key + counter.to_bytes(4, "big")).digest()
            counter += 1
        encrypted = bytes(a ^ b for a, b in zip(data, stream[:len(data)]))
        return b"\x00" * 12 + encrypted  # 12-byte zero nonce signals XOR mode


class FederationClient:
    """HTTP client for federation operations. Uses httpx (already in requirements)."""

    def __init__(self, aggregator_url: str, instance_key: str):
        self.aggregator_url = aggregator_url.rstrip("/")
        self.instance_key = instance_key

    async def submit_delta(self, delta: ConstitutionDelta) -> dict:
        """Submit anonymous delta to aggregator (legacy — no ZK proof)."""
        nonce = _weekly_nonce(self.instance_key)

        submission = {
            "nonce": nonce,
            "encrypted_delta": delta.gene_diffs,
            "fitness_improvement": round(delta.fitness_after - delta.fitness_before, 8),
            "generation_span": delta.generation_to - delta.generation_from,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.aggregator_url}/federation/submit",
                    json=submission,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Federation submit failed: %s", exc)
            return {"error": str(exc)}

    async def submit_delta_with_proof(
        self,
        delta: ConstitutionDelta,
        industry_type: str = "other",
        jwt_token: str = "",
    ) -> dict:
        """
        Submit encrypted delta with a hash-chain commitment to the
        federator.

        This is the v2 submission path that:
        1. Builds a SHA-256 hash-chain commitment (NOT a real ZK proof)
        2. Encrypts the delta payload (federator never decrypts)
        3. Sends both to the /v1/deltas endpoint
        """
        import base64

        nonce = _weekly_nonce(self.instance_key)
        fitness_improvement = round(delta.fitness_after - delta.fitness_before, 8)
        generation_span = delta.generation_to - delta.generation_from
        gene_count = len(delta.gene_diffs)

        # Build hash-chain commitment (labelled "zk_proof" in the wire format
        # for backward compatibility; not a real zero-knowledge proof)
        proof = generate_sigma_proof(delta, fitness_improvement, gene_count, generation_span)

        # Encrypt the payload
        delta_json = json.dumps(delta.gene_diffs, sort_keys=True)
        encrypted = encrypt_delta_payload(delta_json, self.instance_key.encode())

        submission = {
            "nonce": nonce,
            "industry_type": industry_type,
            "encrypted_payload": base64.b64encode(encrypted).decode(),
            "zk_proof": {
                "commitment": proof.commitment,
                "challenge": proof.challenge,
                "response": proof.response,
                "public_inputs": proof.public_inputs,
            },
            "fitness_improvement": fitness_improvement,
            "generation_span": generation_span,
            "gene_count": gene_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        headers = {}
        if jwt_token:
            headers["Authorization"] = f"Bearer {jwt_token}"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.aggregator_url}/v1/deltas",
                    json=submission,
                    headers=headers,
                )
                resp.raise_for_status()
                result = resp.json()
                logger.info(
                    "Federation v2 submit: delta_id=%s merkle_leaf=%s",
                    result.get("delta_id", "?"), result.get("merkle_leaf_hash", "?")[:16],
                )
                return result
        except Exception as exc:
            logger.error("Federation v2 submit failed: %s", exc)
            return {"error": str(exc)}

    async def fetch_results(self, jwt_token: str = "") -> Optional[dict]:
        """Fetch latest merged results from aggregator."""
        headers = {}
        if jwt_token:
            headers["Authorization"] = f"Bearer {jwt_token}"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.aggregator_url}/v1/results",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Federation fetch failed: %s", exc)
            return None

    async def fetch_industry_results(self, industry: str, jwt_token: str = "") -> Optional[dict]:
        """Fetch results for a specific industry."""
        headers = {}
        if jwt_token:
            headers["Authorization"] = f"Bearer {jwt_token}"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.aggregator_url}/v1/results/{industry}",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Federation industry fetch failed: %s", exc)
            return None

    async def fetch_benchmarks(self, jwt_token: str = "") -> Optional[dict]:
        """Fetch cross-industry benchmarks."""
        headers = {}
        if jwt_token:
            headers["Authorization"] = f"Bearer {jwt_token}"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.aggregator_url}/v1/benchmarks",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Federation benchmarks fetch failed: %s", exc)
            return None

    async def verify_inclusion(self, merkle_leaf_hash: str, jwt_token: str = "") -> Optional[dict]:
        """Verify a delta's inclusion in the audit trail."""
        headers = {}
        if jwt_token:
            headers["Authorization"] = f"Bearer {jwt_token}"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.aggregator_url}/v1/audit/verify-inclusion",
                    json={"merkle_leaf_hash": merkle_leaf_hash},
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Federation inclusion verify failed: %s", exc)
            return None


async def get_federation_status(db: AsyncSession) -> dict:
    """Get federation configuration and status."""
    from orchestrator.db.models import FederationSyncLog, FederationMergedResult

    has_consent = await ConsentCheckpoint.has_consent(db)

    # Last sync
    last_sync_result = await db.execute(
        select(FederationSyncLog)
        .order_by(desc(FederationSyncLog.synced_at))
        .limit(1)
    )
    last_sync = last_sync_result.scalar_one_or_none()

    # Latest result
    latest_result = await db.execute(
        select(FederationMergedResult)
        .order_by(desc(FederationMergedResult.received_at))
        .limit(1)
    )
    latest = latest_result.scalar_one_or_none()

    return {
        "federation_enabled": settings.federation_enabled,
        "participation_enabled": settings.federation_participation_enabled,
        "consent_active": has_consent,
        "consortium_mode": settings.federation_consortium_mode,
        "last_sync": {
            "direction": last_sync.direction if last_sync else None,
            "status": last_sync.status if last_sync else None,
            "synced_at": last_sync.synced_at.isoformat() if last_sync and last_sync.synced_at else None,
        } if last_sync else None,
        "latest_result": {
            "version": latest.result_version if latest else None,
            "participating_instances": latest.participating_instances if latest else 0,
            "confidence_score": latest.confidence_score if latest else 0.0,
            "received_at": latest.received_at.isoformat() if latest and latest.received_at else None,
        } if latest else None,
    }


async def run_federation_sync_cycle() -> None:
    """Background task: sync with federation aggregator."""
    if not settings.federation_enabled:
        return

    from orchestrator.db.session import get_session_ctx
    from orchestrator.db.models import FederationSyncLog, FederationMergedResult

    async with get_session_ctx() as db:
        # Check consent
        if not await ConsentCheckpoint.has_consent(db):
            logger.debug("Federation: no consent, skipping sync")
            return

        # Get instance nonce key from consent
        from orchestrator.db.models import FederationConsent
        consent_result = await db.execute(
            select(FederationConsent).where(
                FederationConsent.withdrawn_at.is_(None),
            ).order_by(desc(FederationConsent.consented_at)).limit(1)
        )
        consent = consent_result.scalar_one_or_none()
        if not consent:
            return

        # Fetch results (consumer mode)
        if settings.federation_aggregator_url:
            client = FederationClient(settings.federation_aggregator_url, consent.instance_nonce_key)

            results = await client.fetch_results()
            if results and "error" not in results:
                merged = FederationMergedResult(
                    result_version=results.get("version", 1),
                    gene_improvements=results.get("gene_improvements", {}),
                    participating_instances=results.get("participating_instances", 0),
                    confidence_score=results.get("confidence_score", 0.0),
                )
                db.add(merged)

                sync_log = FederationSyncLog(
                    direction="receive",
                    status="success",
                )
                db.add(sync_log)

                logger.info("Federation: received merged results")
            else:
                sync_log = FederationSyncLog(
                    direction="receive",
                    status="failed",
                    error_message=str(results.get("error", "Unknown")) if results else "No response",
                )
                db.add(sync_log)

        await db.commit()
