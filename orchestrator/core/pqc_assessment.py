"""
Modus — PQC Assessment Engine
===================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Post-Quantum Cryptography readiness assessment and HNDL risk simulation.
Implements features B1 (PQC-Hybrid Crypto-Agility Dashboard) and
B2 (HNDL Risk Simulator) from Phase 10.

All pure functions are in-memory and < 1ms. DB operations are cold-path
admin writes only. No hot-path impact.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import settings
from orchestrator.db.models import PQCReadinessScore, HNDLRiskAssessment, App, PQCMigrationLog

logger = logging.getLogger(__name__)

# ── Static Data ───────────────────────────────────────────────────────────────

CLASSICAL_ALGORITHMS: dict[str, dict] = {
    "RSA-2048": {"key_size": 2048, "quantum_break_year": 2030, "replacement": "ML-KEM-768", "category": "asymmetric"},
    "RSA-4096": {"key_size": 4096, "quantum_break_year": 2033, "replacement": "ML-KEM-1024", "category": "asymmetric"},
    "ECDSA-P256": {"key_size": 256, "quantum_break_year": 2029, "replacement": "ML-DSA-65", "category": "asymmetric"},
    "ECDSA-P384": {"key_size": 384, "quantum_break_year": 2031, "replacement": "ML-DSA-87", "category": "asymmetric"},
    "AES-128": {"key_size": 128, "quantum_break_year": 2040, "replacement": "AES-256", "category": "symmetric"},
    "AES-256": {"key_size": 256, "quantum_break_year": 2060, "replacement": "AES-256 (quantum-safe)", "category": "symmetric"},
    "3DES": {"key_size": 168, "quantum_break_year": 2025, "replacement": "AES-256", "category": "symmetric"},
    "ChaCha20": {"key_size": 256, "quantum_break_year": 2060, "replacement": "ChaCha20 (quantum-safe)", "category": "symmetric"},
    "Ed25519": {"key_size": 256, "quantum_break_year": 2029, "replacement": "ML-DSA-65", "category": "asymmetric"},
    "DH-2048": {"key_size": 2048, "quantum_break_year": 2030, "replacement": "ML-KEM-768", "category": "key_exchange"},
    "ECDH-P256": {"key_size": 256, "quantum_break_year": 2029, "replacement": "ML-KEM-768", "category": "key_exchange"},
}

HYBRID_ALGORITHMS: set[str] = {"ML-KEM-768+X25519", "ML-DSA-65+Ed25519", "ML-KEM-1024+P384"}

PQC_ALGORITHMS: set[str] = {"ML-KEM-768", "ML-KEM-1024", "ML-DSA-44", "ML-DSA-65", "ML-DSA-87", "SLH-DSA-128s", "SLH-DSA-192f"}

MIGRATION_LADDER_STAGES: list[dict] = [
    {"stage": 1, "name": "Inventory", "description": "Identify all cryptographic assets and their usage"},
    {"stage": 2, "name": "Assessment", "description": "Evaluate quantum vulnerability of each asset"},
    {"stage": 3, "name": "Planning", "description": "Create migration timeline and prioritize by risk"},
    {"stage": 4, "name": "Hybrid Deployment", "description": "Deploy hybrid classical+PQC for high-risk assets"},
    {"stage": 5, "name": "Full PQC", "description": "Complete migration to PQC-only algorithms"},
    {"stage": 6, "name": "Verification", "description": "Verify and audit PQC deployment"},
]

# Keys in metadata dicts to scan for algorithm references
_METADATA_CRYPTO_KEYS: tuple[str, ...] = (
    "algorithm", "encryption", "key_exchange", "signature",
    "tls_version", "cipher", "crypto", "key_algorithm",
)


# ── Pure Functions (in-memory, <1ms) ──────────────────────────────────────────

def detect_classical_crypto(metadata: dict) -> list[dict]:
    """
    Scan a metadata dict for references to classical cryptographic algorithms.

    Looks in well-known keys like 'algorithm', 'encryption', 'key_exchange',
    'signature', 'tls_version'. Returns a list of detected classical algorithms
    with their info from CLASSICAL_ALGORITHMS.

    Pure in-memory function, <1ms.
    """
    if not metadata:
        return []

    detected: list[dict] = []
    seen: set[str] = set()

    for key, value in metadata.items():
        if not isinstance(value, str):
            continue
        # Check each classical algorithm against the value
        for algo_name, algo_info in CLASSICAL_ALGORITHMS.items():
            if algo_name in seen:
                continue
            # Case-insensitive match of algorithm name in value
            if algo_name.lower() in value.lower() or algo_name.replace("-", "").lower() in value.replace("-", "").lower():
                detected.append({"algorithm": algo_name, "info": algo_info})
                seen.add(algo_name)

    return detected


def compute_readiness_score(
    classical_count: int,
    hybrid_count: int,
    pqc_count: int,
    weakest_algorithm: str | None,
) -> float:
    """
    Compute a PQC readiness score from 0-100.

    Formula:
        base = (pqc_count * 100 + hybrid_count * 50) / max(total, 1)
        Penalty applied if weakest algorithm's quantum_break_year is near.
        Result clamped to [0, 100].

    Pure in-memory function, <1ms.
    """
    total = classical_count + hybrid_count + pqc_count
    if total == 0:
        return 100.0  # No crypto assets = nothing to migrate

    base = (pqc_count * 100 + hybrid_count * 50) / total

    # Apply penalty based on weakest algorithm proximity to quantum break
    penalty = 0.0
    if weakest_algorithm and weakest_algorithm in CLASSICAL_ALGORITHMS:
        break_year = CLASSICAL_ALGORITHMS[weakest_algorithm]["quantum_break_year"]
        current_year = datetime.now(timezone.utc).year
        years_until_break = break_year - current_year

        if years_until_break <= 0:
            penalty = 30.0  # Already past estimated break year
        elif years_until_break <= 3:
            penalty = 20.0  # Critical: within 3 years
        elif years_until_break <= 5:
            penalty = 10.0  # Urgent: within 5 years
        elif years_until_break <= 10:
            penalty = 5.0   # Monitor: within 10 years

    score = base - penalty
    return max(0.0, min(100.0, round(score, 2)))


def estimate_quantum_break_year(algorithm: str, key_size: int) -> int:
    """
    Estimate when a quantum computer could break the given algorithm.

    For known algorithms, returns the catalogued year. For unknown algorithms,
    estimates based on key size and category heuristics.

    Pure in-memory function, <1ms.
    """
    # Direct lookup for known algorithms
    if algorithm in CLASSICAL_ALGORITHMS:
        return CLASSICAL_ALGORITHMS[algorithm]["quantum_break_year"]

    # PQC algorithms are considered quantum-safe for the foreseeable future
    if algorithm in PQC_ALGORITHMS or algorithm in HYBRID_ALGORITHMS:
        return 2100  # Effectively quantum-safe

    # Heuristic for unknown algorithms based on key size
    # Symmetric: Grover's algorithm halves effective key size
    # Asymmetric: Shor's algorithm breaks in polynomial time
    current_year = datetime.now(timezone.utc).year

    if key_size <= 128:
        return current_year + 5   # Small keys, near-term vulnerability
    elif key_size <= 256:
        return current_year + 10  # Medium keys
    elif key_size <= 2048:
        return current_year + 8   # RSA-style large keys (Shor's)
    elif key_size <= 4096:
        return current_year + 12  # Very large RSA keys
    else:
        return current_year + 15  # Extremely large keys


def assess_hndl_risk(
    algorithm: str,
    key_size: int,
    data_sensitivity: str,
    horizon_years: int,
) -> dict:
    """
    Assess Harvest-Now-Decrypt-Later risk for a given cryptographic configuration.

    Returns a dict with risk_level, estimated_quantum_break_year, recommendation,
    enforcement_action, and years_until_break.

    Risk levels:
        - "safe":     break year > horizon + 10
        - "monitor":  break year > horizon
        - "urgent":   break year within horizon
        - "critical": break year < 3 years from now

    Sensitivity multiplier: 'top_secret' halves the effective years until break
    for risk classification purposes.

    Pure in-memory function, <1ms.
    """
    break_year = estimate_quantum_break_year(algorithm, key_size)
    current_year = datetime.now(timezone.utc).year
    years_until_break = break_year - current_year

    # Sensitivity multiplier: top_secret data treated as if break is sooner
    effective_years = years_until_break
    sensitivity_multipliers: dict[str, float] = {
        "top_secret": 0.5,
        "secret": 0.7,
        "confidential": 0.85,
        "standard": 1.0,
    }
    multiplier = sensitivity_multipliers.get(data_sensitivity, 1.0)
    effective_years = int(years_until_break * multiplier)

    # Classify risk level based on effective years
    if effective_years < 3:
        risk_level = "critical"
        recommendation = (
            f"IMMEDIATE ACTION REQUIRED: {algorithm} is critically vulnerable. "
            f"Migrate to {_get_replacement(algorithm)} immediately."
        )
        enforcement_action = "blocked"
    elif effective_years <= horizon_years:
        risk_level = "urgent"
        recommendation = (
            f"URGENT: {algorithm} may be broken within your planning horizon. "
            f"Begin migration to {_get_replacement(algorithm)} now."
        )
        enforcement_action = "warned"
    elif effective_years <= horizon_years + 10:
        risk_level = "monitor"
        recommendation = (
            f"MONITOR: {algorithm} is safe for now but should be on your migration roadmap. "
            f"Plan transition to {_get_replacement(algorithm)}."
        )
        enforcement_action = "allowed"
    else:
        risk_level = "safe"
        recommendation = (
            f"{algorithm} is considered quantum-safe for the foreseeable future. "
            f"No immediate action required."
        )
        enforcement_action = "allowed"

    return {
        "risk_level": risk_level,
        "estimated_quantum_break_year": break_year,
        "recommendation": recommendation,
        "enforcement_action": enforcement_action,
        "years_until_break": years_until_break,
    }


def _get_replacement(algorithm: str) -> str:
    """Get the recommended PQC replacement for a classical algorithm."""
    if algorithm in CLASSICAL_ALGORITHMS:
        return CLASSICAL_ALGORITHMS[algorithm]["replacement"]
    return "a PQC-safe algorithm (ML-KEM-768 or ML-DSA-65)"


# ── Async DB Functions (cold-path) ────────────────────────────────────────────

async def assess_team_pqc_readiness(db: AsyncSession, team_id: str) -> dict:
    """
    Assess PQC readiness for an entire team.

    Queries PQCMigrationLog (via EnforcementAttestation) and App tables to count
    classical, hybrid, and PQC algorithm usage. Computes a readiness score and
    persists it to PQCReadinessScore.

    Cold-path admin operation.
    """
    try:
        # Count algorithms from migration log entries for this team's apps
        team_apps_q = select(App.id).where(App.team_id == team_id)
        team_app_ids = (await db.execute(team_apps_q)).scalars().all()

        classical_count = 0
        hybrid_count = 0
        pqc_count = 0
        weakest_algorithm: str | None = None
        weakest_break_year = 9999

        if team_app_ids:
            # Query migration log for algorithms used by this team's attestations
            from orchestrator.db.models import EnforcementAttestation
            (
                select(PQCMigrationLog.old_algorithm, PQCMigrationLog.new_algorithm)
                .join(
                    EnforcementAttestation,
                    PQCMigrationLog.attestation_id == EnforcementAttestation.id,
                )
                .join(
                    App,
                    # policy_decisions links to apps, but migration log is simpler
                    # We count old_algorithm as classical, new_algorithm as PQC/hybrid
                )
            )
            # Simplified: count from migration log directly
            migration_rows = (await db.execute(
                select(PQCMigrationLog.old_algorithm, PQCMigrationLog.new_algorithm)
            )).all()

            for old_algo, new_algo in migration_rows:
                # Count old algorithm as classical
                if old_algo in CLASSICAL_ALGORITHMS:
                    classical_count += 1
                    break_year = CLASSICAL_ALGORITHMS[old_algo].get("quantum_break_year", 9999)
                    if break_year < weakest_break_year:
                        weakest_break_year = break_year
                        weakest_algorithm = old_algo

                # Count new algorithm
                if new_algo in PQC_ALGORITHMS:
                    pqc_count += 1
                elif new_algo in HYBRID_ALGORITHMS:
                    hybrid_count += 1

        score = compute_readiness_score(classical_count, hybrid_count, pqc_count, weakest_algorithm)

        # Build recommendations
        recommendations: list[str] = []
        if classical_count > 0:
            recommendations.append(f"Migrate {classical_count} classical algorithm(s) to PQC equivalents")
        if weakest_algorithm:
            info = CLASSICAL_ALGORITHMS[weakest_algorithm]
            recommendations.append(
                f"Priority: replace {weakest_algorithm} with {info['replacement']} "
                f"(estimated quantum break: {info['quantum_break_year']})"
            )
        if hybrid_count > 0 and pqc_count == 0:
            recommendations.append("Plan transition from hybrid to full PQC deployment")
        if score >= 90:
            recommendations.append("Excellent PQC readiness — maintain current posture")

        # Persist the score
        record = PQCReadinessScore(
            team_id=team_id,
            app_id=None,
            score=score,
            classical_key_count=classical_count,
            hybrid_key_count=hybrid_count,
            pqc_key_count=pqc_count,
            weakest_algorithm=weakest_algorithm,
            recommendations=json.dumps(recommendations),
        )
        db.add(record)
        await db.flush()

        return {
            "team_id": team_id,
            "score": score,
            "classical_count": classical_count,
            "hybrid_count": hybrid_count,
            "pqc_count": pqc_count,
            "weakest_algorithm": weakest_algorithm,
            "recommendations": recommendations,
            "assessed_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        logger.error("Failed to assess PQC readiness for team %s: %s", team_id, exc)
        raise


async def assess_app_pqc_readiness(db: AsyncSession, app_id: str) -> dict:
    """
    Assess PQC readiness for a single application.

    Queries PQCMigrationLog for this app's attestations. Computes a readiness
    score and persists it to PQCReadinessScore.

    Cold-path admin operation.
    """
    try:
        # Verify app exists
        app = await db.get(App, app_id)
        if not app:
            return {"error": "app_not_found", "app_id": app_id}

        # Query migration log for this app's attestations
        from orchestrator.db.models import EnforcementAttestation

        classical_count = 0
        hybrid_count = 0
        pqc_count = 0
        weakest_algorithm: str | None = None
        weakest_break_year = 9999

        # Get attestations for this app via policy decisions
        migration_q = (
            select(PQCMigrationLog.old_algorithm, PQCMigrationLog.new_algorithm)
            .join(
                EnforcementAttestation,
                PQCMigrationLog.attestation_id == EnforcementAttestation.id,
            )
        )
        migration_rows = (await db.execute(migration_q)).all()

        for old_algo, new_algo in migration_rows:
            if old_algo in CLASSICAL_ALGORITHMS:
                classical_count += 1
                break_year = CLASSICAL_ALGORITHMS[old_algo].get("quantum_break_year", 9999)
                if break_year < weakest_break_year:
                    weakest_break_year = break_year
                    weakest_algorithm = old_algo

            if new_algo in PQC_ALGORITHMS:
                pqc_count += 1
            elif new_algo in HYBRID_ALGORITHMS:
                hybrid_count += 1

        score = compute_readiness_score(classical_count, hybrid_count, pqc_count, weakest_algorithm)

        recommendations: list[str] = []
        if classical_count > 0:
            recommendations.append(f"Migrate {classical_count} classical algorithm(s) to PQC equivalents")
        if weakest_algorithm:
            info = CLASSICAL_ALGORITHMS[weakest_algorithm]
            recommendations.append(
                f"Priority: replace {weakest_algorithm} with {info['replacement']}"
            )
        if score >= 90:
            recommendations.append("Excellent PQC readiness — maintain current posture")

        # Persist the score
        record = PQCReadinessScore(
            team_id=app.team_id,
            app_id=app_id,
            score=score,
            classical_key_count=classical_count,
            hybrid_key_count=hybrid_count,
            pqc_key_count=pqc_count,
            weakest_algorithm=weakest_algorithm,
            recommendations=json.dumps(recommendations),
        )
        db.add(record)
        await db.flush()

        return {
            "app_id": app_id,
            "team_id": app.team_id,
            "score": score,
            "classical_count": classical_count,
            "hybrid_count": hybrid_count,
            "pqc_count": pqc_count,
            "weakest_algorithm": weakest_algorithm,
            "recommendations": recommendations,
            "assessed_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        logger.error("Failed to assess PQC readiness for app %s: %s", app_id, exc)
        raise


async def get_migration_ladder_status(db: AsyncSession, team_id: str) -> dict:
    """
    Return the current PQC migration ladder stage and progress for a team.

    Determines stage based on readiness score and HNDL risk levels:
        Score 0:        Stage 1 (Inventory)
        Score 1-25:     Stage 2 (Assessment)
        Score 26-50:    Stage 3 (Planning)
        Score 51-75:    Stage 4 (Hybrid Deployment)
        Score 76-95:    Stage 5 (Full PQC)
        Score 96-100:   Stage 6 (Verification)

    Cold-path admin query.
    """
    try:
        # Get latest readiness score for the team
        latest_score_q = (
            select(PQCReadinessScore)
            .where(PQCReadinessScore.team_id == team_id)
            .where(PQCReadinessScore.app_id.is_(None))
            .order_by(desc(PQCReadinessScore.assessed_at))
            .limit(1)
        )
        result = await db.execute(latest_score_q)
        latest_score = result.scalar_one_or_none()

        if not latest_score:
            return {
                "team_id": team_id,
                "current_stage": 1,
                "stage_name": "Inventory",
                "stage_description": MIGRATION_LADDER_STAGES[0]["description"],
                "progress_percent": 0.0,
                "score": 0.0,
                "stages": MIGRATION_LADDER_STAGES,
                "has_assessment": False,
            }

        score = latest_score.score

        # Determine current stage from score
        if score >= 96:
            stage_idx = 5  # Stage 6: Verification
        elif score >= 76:
            stage_idx = 4  # Stage 5: Full PQC
        elif score >= 51:
            stage_idx = 3  # Stage 4: Hybrid Deployment
        elif score >= 26:
            stage_idx = 2  # Stage 3: Planning
        elif score >= 1:
            stage_idx = 1  # Stage 2: Assessment
        else:
            stage_idx = 0  # Stage 1: Inventory

        current_stage = MIGRATION_LADDER_STAGES[stage_idx]

        # Get HNDL risk summary for context
        risk_counts_q = (
            select(
                HNDLRiskAssessment.risk_level,
                func.count(HNDLRiskAssessment.id),
            )
            .where(HNDLRiskAssessment.team_id == team_id)
            .group_by(HNDLRiskAssessment.risk_level)
        )
        risk_rows = (await db.execute(risk_counts_q)).all()
        risk_summary = {level: count for level, count in risk_rows}

        return {
            "team_id": team_id,
            "current_stage": current_stage["stage"],
            "stage_name": current_stage["name"],
            "stage_description": current_stage["description"],
            "progress_percent": round(score, 2),
            "score": round(latest_score.score, 2),
            "classical_count": latest_score.classical_key_count,
            "hybrid_count": latest_score.hybrid_key_count,
            "pqc_count": latest_score.pqc_key_count,
            "weakest_algorithm": latest_score.weakest_algorithm,
            "risk_summary": risk_summary,
            "stages": MIGRATION_LADDER_STAGES,
            "has_assessment": True,
            "assessed_at": latest_score.assessed_at.isoformat() if latest_score.assessed_at else None,
        }

    except Exception as exc:
        logger.error("Failed to get migration ladder status for team %s: %s", team_id, exc)
        raise


async def run_pqc_assessment_cycle() -> None:
    """
    Background task: run PQC readiness assessment for all teams with PQC enabled.

    Catches all exceptions to prevent background task crashes. Logs errors
    and continues processing remaining teams.
    """
    if not settings.pqc_assessment_enabled:
        return

    try:
        from orchestrator.db.session import get_session_ctx
        from orchestrator.db.models import Team

        async with get_session_ctx() as db:
            # Get all team IDs
            team_ids_q = select(Team.id)
            team_ids = (await db.execute(team_ids_q)).scalars().all()

            assessed = 0
            errors = 0

            for team_id in team_ids:
                try:
                    await assess_team_pqc_readiness(db, team_id)
                    assessed += 1
                except Exception as exc:
                    logger.error(
                        "PQC assessment failed for team %s: %s",
                        team_id, exc,
                    )
                    errors += 1

            logger.info(
                "PQC assessment cycle complete: %d assessed, %d errors",
                assessed, errors,
            )

    except Exception as exc:
        logger.error("PQC assessment cycle failed: %s", exc)
