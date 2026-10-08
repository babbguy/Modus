"""
Tests — one authoritative list of policy types.

The orchestrator API list (VALID_POLICY_TYPES) is the source of truth. The
stdlib-only SDK schema mirrors it and the policy engine must have an
evaluator for every type (and none for types the API rejects).
"""
from __future__ import annotations

import re
from pathlib import Path

from modus.policy_schema import POLICY_TYPES as SDK_POLICY_TYPES
from orchestrator.api.policies import BatchPolicyItem, PolicyCreate, VALID_POLICY_TYPES

ROOT = Path(__file__).resolve().parent.parent


def test_sdk_and_api_policy_types_are_equal():
    assert set(SDK_POLICY_TYPES) == set(VALID_POLICY_TYPES)
    assert len(SDK_POLICY_TYPES) == len(set(SDK_POLICY_TYPES)), "SDK list has duplicates"


def test_engine_evaluates_exactly_the_api_types():
    src = (ROOT / "orchestrator" / "core" / "policy_engine.py").read_text(encoding="utf-8")
    handled = set(re.findall(r'policy\.policy_type == "([a-z_]+)"', src))
    assert handled == set(VALID_POLICY_TYPES)


def test_both_api_entry_points_use_the_same_list():
    for ptype in VALID_POLICY_TYPES:
        BatchPolicyItem(name="x", type=ptype)
    for bad in ("webhook", "trajectory_cap", "vigil_check", "nope"):
        try:
            BatchPolicyItem(name="x", type=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"batch accepted {bad}")
        try:
            PolicyCreate(name="x", scope="platform", policy_type=bad, config={})
        except ValueError:
            pass
        else:
            raise AssertionError(f"create accepted {bad}")
