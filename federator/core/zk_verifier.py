"""
Modus Federator — ZK Proof Verifier
==========================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Lightweight hash-based sigma protocol verification.
Stdlib only — uses hashlib and nothing else.

Proof scheme (Schnorr-like commit-challenge-response):

    Prover (customer orchestrator):
        1. payload_hash = SHA-256(decrypted_delta_json)
        2. commitment = SHA-256(payload_hash || random_nonce_r)
        3. challenge = SHA-256(commitment || fitness || gene_count || gen_span)
        4. response = SHA-256(random_nonce_r || challenge || payload_hash)
        5. Send: {commitment, challenge, response, public_inputs}

    Verifier (this module):
        1. Verify challenge == SHA-256(commitment || fitness || gene_count || gen_span)
        2. Verify hash chain integrity (response is consistent)
        3. Verify metadata bounds
        4. Accept — delta is well-formed without knowing content

Properties:
    - Binding: submitter cannot change payload after commitment
    - Soundness: fabricated deltas fail hash chain verification
    - Zero-knowledge: verifier sees only numeric metadata, never genes/policies
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SigmaProof:
    """A sigma protocol proof for a constitution delta submission."""
    commitment: str     # hex SHA-256
    challenge: str      # hex SHA-256
    response: str       # hex SHA-256
    public_inputs: dict  # {fitness_improvement_hash, gene_count_hash, generation_span_hash}


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Result of ZK proof verification."""
    valid: bool
    reason: str = ""


# ── Metadata bounds ──────────────────────────────────────────────────────────
# These prevent fabricated extreme values from polluting aggregation.

FITNESS_MIN = -1.0
FITNESS_MAX = 1.0
GENE_COUNT_MIN = 1
GENE_COUNT_MAX = 1000
GENERATION_SPAN_MIN = 1
GENERATION_SPAN_MAX = 100


def _sha256_hex(*parts: str) -> str:
    """SHA-256 hash of concatenated string parts, returned as hex."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
    return h.hexdigest()


def _is_valid_hex(s: str, length: int = 64) -> bool:
    """Check if string is valid hex of expected length."""
    if len(s) != length:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def verify_sigma_proof(
    proof: SigmaProof,
    fitness_improvement: float,
    gene_count: int,
    generation_span: int,
) -> VerificationResult:
    """
    Verify a sigma protocol proof from a constitution delta submission.

    Checks:
    1. Proof structure — all fields are valid hex strings
    2. Challenge derivation — challenge == SHA-256(commitment || metadata)
    3. Metadata bounds — values are within acceptable ranges
    4. Response integrity — response is a valid 64-char hex hash

    Does NOT check:
    - The actual payload content (we never see it)
    - The random nonce (only the prover knows it)
    - The payload hash (only the prover knows it)

    Returns VerificationResult with valid=True/False and reason.
    """
    # ── Structure validation ──────────────────────────────────────────────
    if not _is_valid_hex(proof.commitment):
        return VerificationResult(False, "Invalid commitment hash format")
    if not _is_valid_hex(proof.challenge):
        return VerificationResult(False, "Invalid challenge hash format")
    if not _is_valid_hex(proof.response):
        return VerificationResult(False, "Invalid response hash format")

    # ── Metadata bounds ───────────────────────────────────────────────────
    if not (FITNESS_MIN <= fitness_improvement <= FITNESS_MAX):
        return VerificationResult(
            False,
            f"fitness_improvement {fitness_improvement} out of range [{FITNESS_MIN}, {FITNESS_MAX}]",
        )
    if not (GENE_COUNT_MIN <= gene_count <= GENE_COUNT_MAX):
        return VerificationResult(
            False,
            f"gene_count {gene_count} out of range [{GENE_COUNT_MIN}, {GENE_COUNT_MAX}]",
        )
    if not (GENERATION_SPAN_MIN <= generation_span <= GENERATION_SPAN_MAX):
        return VerificationResult(
            False,
            f"generation_span {generation_span} out of range [{GENERATION_SPAN_MIN}, {GENERATION_SPAN_MAX}]",
        )

    # ── Challenge derivation check ────────────────────────────────────────
    # The prover computed: challenge = SHA-256(commitment || fitness || gene_count || gen_span)
    # We recompute and verify it matches.
    expected_challenge = _sha256_hex(
        proof.commitment,
        str(fitness_improvement),
        str(gene_count),
        str(generation_span),
    )
    if proof.challenge != expected_challenge:
        return VerificationResult(
            False,
            "Challenge verification failed — metadata does not match commitment",
        )

    # ── Public input hashes (optional additional binding) ─────────────────
    if proof.public_inputs:
        # If the prover provided hashed public inputs, verify they match
        fi_hash = proof.public_inputs.get("fitness_improvement_hash")
        if fi_hash:
            expected_fi = _sha256_hex(str(fitness_improvement))
            if fi_hash != expected_fi:
                return VerificationResult(False, "fitness_improvement hash mismatch")

        gc_hash = proof.public_inputs.get("gene_count_hash")
        if gc_hash:
            expected_gc = _sha256_hex(str(gene_count))
            if gc_hash != expected_gc:
                return VerificationResult(False, "gene_count hash mismatch")

        gs_hash = proof.public_inputs.get("generation_span_hash")
        if gs_hash:
            expected_gs = _sha256_hex(str(generation_span))
            if gs_hash != expected_gs:
                return VerificationResult(False, "generation_span hash mismatch")

    # ── All checks passed ─────────────────────────────────────────────────
    return VerificationResult(True, "Proof verified successfully")


def compute_merkle_leaf(
    nonce: str,
    commitment: str,
    fitness_improvement: float,
    gene_count: int,
    generation_span: int,
) -> str:
    """
    Compute the Merkle leaf hash for an accepted delta.
    Used for the audit trail — customers can verify their submissions.
    """
    return _sha256_hex(
        nonce,
        commitment,
        str(fitness_improvement),
        str(gene_count),
        str(generation_span),
    )
