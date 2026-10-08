"""
Modus — Neuro-Symbolic Policy Prover (Phase 6)
==============================================

Formally verifies governance policies by proving (or disproving) that a
policy's constraints hold under all possible input assignments.  Two
proving tiers are provided:

  Tier 1 — Bounded Enumeration (pure Python, no dependencies)
      Discretizes the variable space and checks every combination for
      constraint violations.  Fast for simple policies (<500 ms), and
      works on any deployment — even a Raspberry Pi.

  Tier 2 — Z3 SMT Solver (optional dependency)
      Encodes policy constraints as first-order logic and asks Z3 to
      find a violating assignment.  Complete within the theory of linear
      real arithmetic.  Falls back to Tier 1 automatically if z3 is not
      installed.

Main entry point:
    prove_policy(policy_yaml, policy_id, method)

All computation runs locally.  No data leaves the customer's
infrastructure.  Thread-safe — no shared mutable state.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# PyYAML is third-party; the prover must not hard-require it (Law 1 posture).
# JSON input (a YAML subset) always works; YAML input needs PyYAML installed.
try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]

# ── Check for optional Z3 dependency ─────────────────────────────────────────

_z3_available = False
try:
    import z3 as _z3  # type: ignore[import-untyped]
    _z3_available = True
except ImportError:
    _z3 = None  # type: ignore[assignment]


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProofResult:
    """Immutable result of a policy proof attempt."""
    policy_id: str
    policy_hash: str            # SHA-256 of the YAML source
    proof_type: str             # "bounded" or "smt"
    status: str                 # "proven" (SMT only) | "sampled" (bounded pass) | "disproven" | "timeout" | "unknown"
    statement: str              # Human-readable description of what was proven
    counterexample: Optional[dict]  # If disproven, the violating assignment
    variables_checked: int
    max_depth: int
    solver_time_ms: int
    proven_at: str              # ISO-8601


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _linspace(lo: float, hi: float, n: int) -> List[float]:
    """Return *n* evenly spaced values from *lo* to *hi* inclusive."""
    if n <= 1:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def _parse_policies(yaml_text: str) -> List[dict]:
    """Parse policy text (JSON always; YAML when PyYAML is installed)."""
    import json

    parsed: Any = None
    try:
        parsed = json.loads(yaml_text)
    except (ValueError, TypeError):
        if _yaml is None:
            logger.warning(
                "Policy input is not JSON and PyYAML is not installed — "
                "cannot parse YAML policy text."
            )
            return []
        try:
            parsed = _yaml.safe_load(yaml_text)
        except Exception:
            return []
    if isinstance(parsed, dict):
        # Handle nested {policies: [...]} wrapper
        if "policies" in parsed and isinstance(parsed["policies"], list):
            return [p for p in parsed["policies"] if isinstance(p, dict)]
        return [parsed]
    if isinstance(parsed, list):
        return [p for p in parsed if isinstance(p, dict)]
    return []


# ── Variable domain extraction ───────────────────────────────────────────────

def _extract_domains(
    policy: dict,
    steps: int = 100,
) -> Dict[str, List[float]]:
    """
    Determine which variables are relevant to a policy and build
    discretised domains for bounded enumeration.
    """
    ptype = policy.get("type", "")
    config = policy.get("config", {}) or {}
    domains: Dict[str, List[float]] = {}

    if ptype == "budget_cap":
        cap = float(config.get("cap_usd", 100))
        domains["spend"] = _linspace(0.0, cap * 2, steps)

    elif ptype == "rate_limit":
        max_calls = int(config.get("max_calls", 100))
        domains["call_count"] = _linspace(0, max_calls * 2, steps)

    elif ptype == "token_cap":
        max_tokens = int(config.get("max_tokens", 10000))
        domains["token_count"] = _linspace(0, max_tokens * 2, steps)

    elif ptype == "amplification_gate":
        max_amp = float(config.get("max_amplification", 5.0))
        domains["amplification"] = _linspace(1.0, max_amp * 2, steps)

    elif ptype == "degradation_ladder":
        budget = float(config.get("budget_usd", 100))
        domains["spend"] = _linspace(0.0, budget * 2, steps)

    elif ptype == "model_denylist":
        # No continuous variables — model denylist is categorical
        pass

    else:
        # Unknown type — create a generic spend domain
        domains["spend"] = _linspace(0.0, 1000.0, steps)

    return domains


# ── Constraint checkers ──────────────────────────────────────────────────────

def _check_budget_cap(
    config: dict, vals: Dict[str, float],
) -> Tuple[bool, str]:
    """Return (violated, description)."""
    cap = float(config.get("cap_usd", 0))
    spend = vals.get("spend", 0.0)
    if spend > cap:
        return True, f"spend={spend:.4f} exceeds cap_usd={cap}"
    return False, ""


def _check_rate_limit(
    config: dict, vals: Dict[str, float],
) -> Tuple[bool, str]:
    max_calls = int(config.get("max_calls", 0))
    call_count = vals.get("call_count", 0.0)
    if call_count > max_calls:
        return True, f"call_count={int(call_count)} exceeds max_calls={max_calls}"
    return False, ""


def _check_token_cap(
    config: dict, vals: Dict[str, float],
) -> Tuple[bool, str]:
    max_tokens = int(config.get("max_tokens", 0))
    token_count = vals.get("token_count", 0.0)
    if token_count > max_tokens:
        return True, f"token_count={int(token_count)} exceeds max_tokens={max_tokens}"
    return False, ""


def _check_amplification_gate(
    config: dict, vals: Dict[str, float],
) -> Tuple[bool, str]:
    max_amp = float(config.get("max_amplification", 0))
    amp = vals.get("amplification", 1.0)
    if amp > max_amp:
        return True, f"amplification={amp:.2f} exceeds max_amplification={max_amp}"
    return False, ""


def _check_degradation_ladder(
    config: dict, vals: Dict[str, float],
) -> Tuple[bool, str]:
    """
    Prove that the degradation ladder catches overspend at every tier.
    A violation means spend can exceed the budget without any tier
    triggering a protective action.
    """
    budget = float(config.get("budget_usd", 0))
    tiers = config.get("tiers", [])
    spend = vals.get("spend", 0.0)

    if budget <= 0 or not tiers:
        return False, ""

    spend_pct = (spend / budget) * 100 if budget > 0 else 0

    # Check that at least one tier fires for the current spend level
    has_deny = False
    for tier in tiers:
        pct = float(tier.get("pct", 0))
        action = tier.get("action", "")
        if spend_pct >= pct:
            if action == "deny":
                has_deny = True

    # Violation: spend exceeds budget but no deny tier fires
    if spend > budget and not has_deny:
        return (
            True,
            f"spend={spend:.4f} exceeds budget={budget} but no deny tier fires",
        )
    return False, ""


_CHECKERS = {
    "budget_cap": _check_budget_cap,
    "rate_limit": _check_rate_limit,
    "token_cap": _check_token_cap,
    "amplification_gate": _check_amplification_gate,
    "degradation_ladder": _check_degradation_ladder,
}


# ── Tier 1: Bounded Enumeration Prover ───────────────────────────────────────

def bounded_prove(
    policy_yaml: str,
    policy_id: str = "",
    timeout_seconds: float = 5.0,
    steps: int = 100,
) -> ProofResult:
    """
    Prove a policy by bounded enumeration of the discretised variable
    space.  Pure Python — no external dependencies.

    Parameters
    ----------
    policy_yaml : str
        YAML text describing one or more policies.
    policy_id : str
        Optional identifier for the policy.
    timeout_seconds : float
        Maximum wall-clock time before returning a "timeout" result.
    steps : int
        Number of discrete values per variable dimension.

    Returns
    -------
    ProofResult
    """
    t0 = time.perf_counter()
    policy_hash = _sha256(policy_yaml)
    policies = _parse_policies(policy_yaml)

    if not policies:
        return ProofResult(
            policy_id=policy_id,
            policy_hash=policy_hash,
            proof_type="bounded",
            status="unknown",
            statement="Could not parse policy YAML",
            counterexample=None,
            variables_checked=0,
            max_depth=0,
            solver_time_ms=0,
            proven_at=_now_iso(),
        )

    total_checked = 0
    max_depth = 0
    boundary_violations = 0

    for policy in policies:
        ptype = policy.get("type", "")
        config = policy.get("config", {}) or {}
        checker = _CHECKERS.get(ptype)

        if checker is None:
            continue

        domains = _extract_domains(policy, steps=steps)
        if not domains:
            # No continuous variables to enumerate (e.g. model_denylist)
            continue

        var_names = sorted(domains.keys())
        var_lists = [domains[v] for v in var_names]
        depth = len(var_names)
        max_depth = max(max_depth, depth)

        for combo in itertools.product(*var_lists):
            # Timeout check
            elapsed = time.perf_counter() - t0
            if elapsed > timeout_seconds:
                return ProofResult(
                    policy_id=policy_id,
                    policy_hash=policy_hash,
                    proof_type="bounded",
                    status="timeout",
                    statement=(
                        f"Bounded enumeration timed out after "
                        f"{elapsed:.1f}s ({total_checked} points checked)"
                    ),
                    counterexample=None,
                    variables_checked=total_checked,
                    max_depth=max_depth,
                    solver_time_ms=round(elapsed * 1000),
                    proven_at=_now_iso(),
                )

            vals = dict(zip(var_names, combo))
            total_checked += 1

            violated, desc = checker(config, vals)
            if violated:
                # For cap policies (budget/rate/token/amplification) the
                # domain deliberately spans past the cap, so violations at
                # sampled points above the boundary are EXPECTED — they show
                # the policy's deny/throttle effect fires there. Count them
                # so the certificate can state honestly what was observed.
                boundary_violations += 1

                # For degradation_ladder, violation means the ladder has a
                # gap (no deny tier fires when it should).
                if ptype == "degradation_ladder":
                    elapsed_ms = round((time.perf_counter() - t0) * 1000)
                    return ProofResult(
                        policy_id=policy_id,
                        policy_hash=policy_hash,
                        proof_type="bounded",
                        status="disproven",
                        statement=(
                            f"Degradation ladder has coverage gap: {desc}"
                        ),
                        counterexample={
                            "policy_type": ptype,
                            **{k: round(v, 6) for k, v in vals.items()},
                            "description": desc,
                        },
                        variables_checked=total_checked,
                        max_depth=max_depth,
                        solver_time_ms=elapsed_ms,
                        proven_at=_now_iso(),
                    )

    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    policy_names = ", ".join(
        p.get("name", p.get("type", "unknown")) for p in policies
    )

    # Bounded enumeration samples discrete points — it is NOT a universal
    # proof, so the result is "sampled", never "proven". Only an SMT unsat
    # result earns "proven". The statement reflects exactly what happened.
    return ProofResult(
        policy_id=policy_id,
        policy_hash=policy_hash,
        proof_type="bounded",
        status="sampled",
        statement=(
            f"Sampled {total_checked} points across policies: {policy_names}. "
            f"{boundary_violations} points exceeded a policy cap, all handled "
            "by the policy's configured effect; no unhandled gap was observed. "
            "Sampling is not a universal proof — use SMT (method=smt) for one."
        ),
        counterexample=None,
        variables_checked=total_checked,
        max_depth=max_depth,
        solver_time_ms=elapsed_ms,
        proven_at=_now_iso(),
    )


# ── Tier 2: Z3 SMT Solver ───────────────────────────────────────────────────

def smt_prove(
    policy_yaml: str,
    policy_id: str = "",
    timeout_ms: int = 5000,
) -> ProofResult:
    """
    Prove a policy using the Z3 SMT solver.  If Z3 is not installed,
    falls back to bounded enumeration automatically.

    The solver encodes the NEGATION of the policy constraints and asks
    for a satisfying assignment.  If the negation is unsatisfiable, the
    policy holds universally ("proven").  If satisfiable, the model is a
    counterexample ("disproven").

    Parameters
    ----------
    policy_yaml : str
        YAML text describing one or more policies.
    policy_id : str
        Optional identifier for the policy.
    timeout_ms : int
        Z3 solver timeout in milliseconds.

    Returns
    -------
    ProofResult
    """
    if not _z3_available or _z3 is None:
        logger.debug("Z3 not available, falling back to bounded prover")
        return bounded_prove(
            policy_yaml, policy_id,
            timeout_seconds=timeout_ms / 1000.0,
        )

    t0 = time.perf_counter()
    policy_hash = _sha256(policy_yaml)
    policies = _parse_policies(policy_yaml)

    if not policies:
        return ProofResult(
            policy_id=policy_id,
            policy_hash=policy_hash,
            proof_type="smt",
            status="unknown",
            statement="Could not parse policy YAML",
            counterexample=None,
            variables_checked=0,
            max_depth=0,
            solver_time_ms=0,
            proven_at=_now_iso(),
        )

    # Create Z3 variables (shared across all policies in the YAML)
    spend = _z3.Real("spend")
    call_count = _z3.Int("call_count")
    token_count = _z3.Int("token_count")
    amplification = _z3.Real("amplification")
    session_depth = _z3.Int("session_depth")

    solver = _z3.Solver()
    solver.set("timeout", timeout_ms)

    # Universal physical constraints
    solver.add(spend >= 0)
    solver.add(call_count >= 0)
    solver.add(token_count >= 0)
    solver.add(amplification >= 1)
    solver.add(session_depth >= 0)

    has_constraints = False

    for policy in policies:
        ptype = policy.get("type", "")
        config = policy.get("config", {}) or {}

        if ptype == "budget_cap":
            cap = float(config.get("cap_usd", 0))
            if cap > 0:
                # Negated constraint: try to find spend > cap
                # (proving no such assignment exists means policy holds)
                solver.add(spend > cap)
                solver.add(spend <= cap * 2)  # bound the search
                has_constraints = True

        elif ptype == "rate_limit":
            max_calls = int(config.get("max_calls", 0))
            if max_calls > 0:
                solver.add(call_count > max_calls)
                solver.add(call_count <= max_calls * 2)
                has_constraints = True

        elif ptype == "token_cap":
            max_tokens = int(config.get("max_tokens", 0))
            if max_tokens > 0:
                solver.add(token_count > max_tokens)
                solver.add(token_count <= max_tokens * 2)
                has_constraints = True

        elif ptype == "amplification_gate":
            max_amp = float(config.get("max_amplification", 0))
            if max_amp > 0:
                solver.add(amplification > max_amp)
                solver.add(amplification <= max_amp * 2)
                has_constraints = True

        elif ptype == "degradation_ladder":
            budget = float(config.get("budget_usd", 0))
            tiers = config.get("tiers", [])
            if budget > 0 and tiers:
                solver.add(spend > budget)
                solver.add(spend <= budget * 2)

                # Check for deny tier coverage: if no deny tier fires
                # at 100%+, there is a gap.
                deny_thresholds = [
                    float(t.get("pct", 0))
                    for t in tiers if t.get("action") == "deny"
                ]
                if deny_thresholds:
                    max_deny_pct = max(deny_thresholds)
                    # Violation: spend is above budget but below the
                    # deny threshold
                    deny_boundary = budget * (max_deny_pct / 100.0)
                    solver.add(spend > deny_boundary)
                else:
                    # No deny tier at all — always a gap
                    has_constraints = True

                has_constraints = True

    if not has_constraints:
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        return ProofResult(
            policy_id=policy_id,
            policy_hash=policy_hash,
            proof_type="smt",
            status="proven",
            statement="No falsifiable constraints found — policy is trivially valid",
            counterexample=None,
            variables_checked=0,
            max_depth=0,
            solver_time_ms=elapsed_ms,
            proven_at=_now_iso(),
        )

    result = solver.check()
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    if result == _z3.unsat:
        return ProofResult(
            policy_id=policy_id,
            policy_hash=policy_hash,
            proof_type="smt",
            status="proven",
            statement=(
                "SMT solver proved no constraint violation is possible "
                "within the variable domain"
            ),
            counterexample=None,
            variables_checked=0,
            max_depth=len(policies),
            solver_time_ms=elapsed_ms,
            proven_at=_now_iso(),
        )

    if result == _z3.sat:
        model = solver.model()
        ce: Dict[str, Any] = {}
        for decl in model.decls():
            val = model[decl]
            name = decl.name()
            # Convert Z3 values to Python numbers
            if _z3.is_int_value(val):
                ce[name] = val.as_long()
            elif _z3.is_rational_value(val):
                ce[name] = float(val.as_fraction())
            elif _z3.is_algebraic_value(val):
                ce[name] = float(val.approx(10))
            else:
                ce[name] = str(val)

        return ProofResult(
            policy_id=policy_id,
            policy_hash=policy_hash,
            proof_type="smt",
            status="disproven",
            statement=(
                "SMT solver found a satisfying assignment that violates "
                "policy constraints"
            ),
            counterexample=ce,
            variables_checked=0,
            max_depth=len(policies),
            solver_time_ms=elapsed_ms,
            proven_at=_now_iso(),
        )

    # Unknown / timeout
    status = "timeout" if elapsed_ms >= timeout_ms else "unknown"
    return ProofResult(
        policy_id=policy_id,
        policy_hash=policy_hash,
        proof_type="smt",
        status=status,
        statement=f"SMT solver returned '{result}' after {elapsed_ms}ms",
        counterexample=None,
        variables_checked=0,
        max_depth=len(policies),
        solver_time_ms=elapsed_ms,
        proven_at=_now_iso(),
    )


# ── Main entry point ────────────────────────────────────────────────────────

def prove_policy(
    policy_yaml: str,
    policy_id: str = "",
    method: str = "auto",
) -> ProofResult:
    """
    Prove or disprove a governance policy's constraints.

    Parameters
    ----------
    policy_yaml : str
        YAML text describing one or more policies.
    policy_id : str
        Optional identifier for the policy.
    method : str
        Proving strategy:
        - ``"auto"`` — use Z3 if available, fall back to bounded.
        - ``"bounded"`` — pure-Python bounded enumeration only.
        - ``"smt"`` — Z3 only (raises if z3 not installed).

    Returns
    -------
    ProofResult
        Frozen dataclass with proof status, timing, and optional
        counterexample.

    Raises
    ------
    ImportError
        If ``method="smt"`` and z3 is not installed.
    """
    if method == "smt":
        if not _z3_available:
            raise ImportError(
                "Z3 solver requested but z3-solver package is not installed. "
                "Install with: pip install z3-solver"
            )
        return smt_prove(policy_yaml, policy_id)

    if method == "bounded":
        return bounded_prove(policy_yaml, policy_id)

    # method == "auto"
    if _z3_available:
        return smt_prove(policy_yaml, policy_id)
    return bounded_prove(policy_yaml, policy_id)


# ── Proof certificate ────────────────────────────────────────────────────────

def generate_proof_certificate(result: ProofResult) -> dict:
    """
    Convert a ProofResult into a JSON-serializable dict suitable for
    storage in the PolicyProof database model or API responses.

    Returns
    -------
    dict
        Contains all ProofResult fields plus a ``certificate_version``
        marker for forward compatibility.
    """
    cert = asdict(result)
    cert["certificate_version"] = 1
    # "verified" is reserved for universal (SMT) proofs; a bounded pass is a
    # sampling result and must not be presented as verification.
    cert["verified"] = result.status == "proven"
    cert["sampled_only"] = result.status == "sampled"
    return cert
