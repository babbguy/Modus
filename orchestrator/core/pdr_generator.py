"""
Modus — Policy Decision Record (PDR) Generator
=====================================================
Cryptographically signed records of MPC-evaluated swarm decisions.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def generate_pdr(
    team_id: str,
    swarm_id: str,
    session_id: str,
    mpc_result: dict,
    mpc_proof: Optional[str] = None,
) -> dict:
    """Generate a Policy Decision Record with cryptographic attestation."""
    pdr_data = {
        "team_id": team_id,
        "swarm_id": swarm_id,
        "session_id": session_id,
        "decision": mpc_result.get("decision", "deny"),
        "party_count": mpc_result.get("party_count", 0),
        "threshold_met": mpc_result.get("threshold_met", False),
        "mpc_proof": mpc_proof,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }

    # Sign with the attestation engine. The signing key comes from the real
    # key resolver (MODUS_ATTESTATION_KEY / MODUS_ENCRYPTION_KEY); the
    # previous code read a nonexistent settings.attestation_hmac_key, so the
    # signature was always None and verify_pdr passed on presence alone.
    #
    # The FULL signed attestation payload (including the random nonce) is stored
    # so verify_pdr can reconstruct and check it exactly; without the nonce the
    # signature would be unverifiable.
    attestation = _sign_pdr(pdr_data, swarm_id, team_id)
    pdr_data["attestation"] = attestation
    pdr_data["attestation_signature"] = (
        attestation.get("signature") if attestation else None
    )
    return pdr_data


def _decision_id(pdr_data: dict) -> str:
    """Stable decision id over the PDR content (excluding attestation fields)."""
    core = {
        k: v for k, v in pdr_data.items()
        if k not in ("attestation_signature", "attestation")
    }
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _sign_pdr(pdr_data: dict, swarm_id: str, team_id: str) -> Optional[dict]:
    try:
        from orchestrator.core.attestation_engine import (
            AttestationSigner, resolve_attestation_key,
        )
        key = resolve_attestation_key()
        if not key:
            logger.warning(
                "PDR unsigned: no attestation key configured "
                "(set MODUS_ATTESTATION_KEY or MODUS_ENCRYPTION_KEY)."
            )
            return None
        signer = AttestationSigner(key)
        return signer.sign_decision(
            decision_id=_decision_id(pdr_data),
            decision=pdr_data["decision"],
            policy_id="swarm_governance",
            policy_type="mpc",
            app_id=swarm_id,
            team_id=team_id,
            timestamp=pdr_data["issued_at"],
        )
    except Exception as exc:
        logger.warning("PDR signing failed: %s", exc)
        return None


def verify_pdr(pdr_data: dict) -> dict:
    """Verify a PDR's cryptographic integrity.

    ``valid`` now requires a signature that actually verifies against the
    configured attestation key — not the mere *presence* of a signature or an
    unverified MPC proof, which the previous implementation accepted. It also
    binds the signature to the PDR content: if the decision was altered after
    signing, the recomputed decision_id no longer matches and verification
    fails.
    """
    from orchestrator.core.attestation_engine import (
        AttestationSigner, resolve_attestation_key,
    )

    attestation = pdr_data.get("attestation")
    has_signature = bool(attestation and attestation.get("signature"))
    has_proof = pdr_data.get("mpc_proof") is not None
    signature_valid = False
    verify_note = None

    if has_signature:
        key = resolve_attestation_key()
        if not key:
            verify_note = "signature present but no key configured to verify it"
        else:
            try:
                signer = AttestationSigner(key)
                # (1) HMAC over the stored attestation payload must be valid.
                hmac_ok = signer.verify_attestation(
                    attestation, attestation["signature"]
                )
                # (2) The attestation must be bound to THIS PDR's content: the
                # decision_id it signed must equal the id recomputed from the
                # current PDR fields. This catches post-signing tampering.
                bound = attestation.get("decision_id") == _decision_id(pdr_data)
                signature_valid = bool(hmac_ok and bound)
                if hmac_ok and not bound:
                    verify_note = "signature valid but does not match PDR content (tampered)"
            except Exception as exc:
                verify_note = f"verification error: {exc}"

    return {
        "valid": signature_valid,
        "signature_present": has_signature,
        "signature_valid": signature_valid,
        "proof_present": has_proof,
        "verify_note": verify_note,
        "decision": pdr_data.get("decision"),
        "party_count": pdr_data.get("party_count"),
        "threshold_met": pdr_data.get("threshold_met"),
    }
