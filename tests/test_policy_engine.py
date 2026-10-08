"""
Tests for Modus policy engine, session tokens, and key cache.

Stdlib only — no pytest, no pydantic, no bcrypt, no SQLAlchemy required.
Loads modules directly from source using importlib to bypass the dep chain.

Run:
    python -m unittest tests/test_policy_engine.py -v      (from the repository root)
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
import unittest
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

_repo = os.path.join(os.path.dirname(__file__), "..")
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ── Fake ORM model classes ─────────────────────────────────────────────────────

@dataclass
class FakeApp:
    id: str
    team_id: str
    is_active: bool = True
    deleted_at: object = None
    enforcement_state: str = "active"
    enforcement_suspended_reason: Optional[str] = None

@dataclass
class FakePolicy:
    id: str
    name: str
    policy_type: str
    scope: str
    effect: str
    priority: int
    conditions: Optional[dict]
    config: Optional[dict]
    action: Optional[dict]
    is_active: bool = True
    team_id: Optional[str] = None
    app_id: Optional[str] = None

@dataclass
class _ColMock:
    """Minimal stand-in for a SQLAlchemy mapped column.
    Supports == and != comparisons, which is all the policy engine needs
    for WHERE clause construction.
    """
    def __init__(self, name):
        self._name = name
    def __eq__(self, other):
        return MagicMock()
    def __ne__(self, other):
        return MagicMock()
    def __repr__(self):
        return f"<ColMock {self._name}>"


@dataclass
class FakeRTS:
    # Instance attributes (used in test assertions)
    app_id: str = ""
    period: str = ""
    window_key: str = ""
    total_cost: Decimal = Decimal("0")
    call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

# Class-level column attributes — policy engine does RealTimeSpend.app_id == x
# which SQLAlchemy handles at the class level. FakeRTS needs the same interface.
FakeRTS.app_id = _ColMock("app_id")
FakeRTS.period = _ColMock("period")
FakeRTS.window_key = _ColMock("window_key")


# ── Load session_token directly from source, injecting a fake config ──────────

def _load_session_token():
    """Load session_token.py without needing pydantic or the full config system."""
    MASTER_KEY = "test-master-key-for-tests-only"

    # Build a minimal fake settings object
    fake_settings = MagicMock()
    fake_settings.master_api_key = MASTER_KEY

    # Save originals so we can restore after loading
    _saved = {k: sys.modules.get(k) for k in [
        "orchestrator", "orchestrator.core", "orchestrator.core.config",
    ]}

    # Stub out orchestrator.core.config before the module loads
    cfg_mod = types.ModuleType("orchestrator.core.config")
    cfg_mod.settings = fake_settings
    sys.modules["orchestrator"] = types.ModuleType("orchestrator")
    sys.modules["orchestrator.core"] = types.ModuleType("orchestrator.core")
    sys.modules["orchestrator.core.config"] = cfg_mod

    path = os.path.join(_repo, "orchestrator", "core", "session_token.py")
    spec = importlib.util.spec_from_file_location("_session_token", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore original modules so other tests aren't poisoned
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

    return mod

_st = _load_session_token()
gen_token = _st.generate
verify_token = _st.verify
is_session_token = _st.is_session_token
SessionTokenError = _st.SessionTokenError
SESSION_TOKEN_PREFIX = _st.SESSION_TOKEN_PREFIX


# ── Load policy_engine directly from source, injecting fake models ────────────

def _load_policy_engine():
    """Load policy_engine.py without needing SQLAlchemy ORM or the orchestrator package."""
    # Save originals so we can restore after loading
    _stub_keys = [
        "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio", "sqlalchemy.orm",
        "orchestrator", "orchestrator.db", "orchestrator.db.models",
        "orchestrator.core", "orchestrator.core.config",
    ]
    _saved = {k: sys.modules.get(k) for k in _stub_keys}

    # Stub sqlalchemy.select to return a chainable mock
    sa_mod = types.ModuleType("sqlalchemy")
    _chain = MagicMock()
    _chain.where = MagicMock(return_value=_chain)
    _chain.order_by = MagicMock(return_value=_chain)
    sa_mod.select = MagicMock(return_value=_chain)
    sys.modules["sqlalchemy"] = sa_mod

    sa_ext = types.ModuleType("sqlalchemy.ext")
    sa_ext_async = types.ModuleType("sqlalchemy.ext.asyncio")
    sa_ext_async.AsyncSession = MagicMock
    sys.modules["sqlalchemy.ext"] = sa_ext
    sys.modules["sqlalchemy.ext.asyncio"] = sa_ext_async
    sys.modules["sqlalchemy.orm"] = types.ModuleType("sqlalchemy.orm")

    # Stub orchestrator package hierarchy so the top-level import
    #   from orchestrator.db.models import App, GovernancePolicy, ...
    # resolves without touching the real package (which requires SQLAlchemy etc.)
    orch_mod = types.ModuleType("orchestrator")
    orch_db = types.ModuleType("orchestrator.db")
    orch_models = types.ModuleType("orchestrator.db.models")
    orch_models.App = FakeApp
    orch_models.GovernancePolicy = FakePolicy
    orch_models.PolicyDecision = MagicMock
    orch_models.RealTimeSpend = FakeRTS
    orch_core = types.ModuleType("orchestrator.core")
    orch_config = types.ModuleType("orchestrator.core.config")
    orch_config.settings = MagicMock()
    sys.modules["orchestrator"] = orch_mod
    sys.modules["orchestrator.db"] = orch_db
    sys.modules["orchestrator.db.models"] = orch_models
    sys.modules["orchestrator.core"] = orch_core
    sys.modules["orchestrator.core.config"] = orch_config

    path = os.path.join(_repo, "orchestrator", "core", "policy_engine.py")
    spec = importlib.util.spec_from_file_location("_pe", path)
    mod = importlib.util.module_from_spec(spec)

    # Must be in sys.modules before exec_module — dataclass decorator
    # does sys.modules[cls.__module__].__dict__ at class definition time.
    sys.modules["_pe"] = mod

    # Belt-and-suspenders: also inject into the module dict directly
    mod.__dict__.update({
        "App": FakeApp,
        "GovernancePolicy": FakePolicy,
        "PolicyDecision": MagicMock,
        "RealTimeSpend": FakeRTS,
    })
    spec.loader.exec_module(mod)

    # Restore original modules so other tests aren't poisoned
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

    return mod

_pe = _load_policy_engine()
_window_key = _pe._window_key
_conditions_match = _pe._conditions_match
_evaluate_policy = _pe._evaluate_policy
EvaluateRequest = _pe.EvaluateRequest
PolicyResult = _pe.PolicyResult


# ── Test helpers ───────────────────────────────────────────────────────────────

def _uuid(): return str(uuid.uuid4())
def _now(): return datetime.now(timezone.utc)

def _req(**kw):
    d = dict(app_id=_uuid(), team_id=_uuid(), provider="anthropic",
             model="claude-sonnet-4-6", environment="production",
             resource_type="llm_call", estimated_tokens=1000,
             estimated_cost=Decimal("0.003"))
    d.update(kw)
    return EvaluateRequest(**d)

def _policy(policy_type, *, effect="deny", scope="platform", priority=100,
            conditions=None, config=None, action=None, app_id=None, team_id=None):
    return FakePolicy(id=_uuid(), name=f"test-{policy_type}",
                      policy_type=policy_type, scope=scope, effect=effect,
                      priority=priority, conditions=conditions,
                      config=config or {}, action=action or {},
                      app_id=app_id, team_id=team_id)

def _mock_db(rts_row=None):
    db = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = rts_row
    db.execute.return_value = res
    return db

def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════════
# _window_key
# ══════════════════════════════════════════════════════════════════════════════

class TestWindowKey(unittest.TestCase):

    def test_hourly_format(self):
        dt = datetime(2026, 3, 2, 14, 37, 22, tzinfo=timezone.utc)
        key, start, end = _window_key("hourly", dt)
        self.assertEqual(key, "hourly:2026-03-02T14")
        self.assertEqual(start, datetime(2026, 3, 2, 14, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc))

    def test_daily_format(self):
        dt = datetime(2026, 3, 2, 14, 37, tzinfo=timezone.utc)
        key, start, end = _window_key("daily", dt)
        self.assertEqual(key, "daily:2026-03-02")
        self.assertEqual(start, datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 3, 3, 0, 0, 0, tzinfo=timezone.utc))

    def test_monthly_format(self):
        dt = datetime(2026, 3, 15, tzinfo=timezone.utc)
        key, start, end = _window_key("monthly", dt)
        self.assertEqual(key, "monthly:2026-03")
        self.assertEqual(start, datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc))

    def test_december_rolls_to_january(self):
        dt = datetime(2026, 12, 25, tzinfo=timezone.utc)
        _, _, end = _window_key("monthly", dt)
        self.assertEqual(end, datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

    def test_unknown_period_raises(self):
        with self.assertRaises(ValueError):
            _window_key("weekly", _now())

    def test_late_minute_stays_in_same_hour_window(self):
        dt = datetime(2026, 3, 2, 14, 59, 59, tzinfo=timezone.utc)
        key, _, _ = _window_key("hourly", dt)
        self.assertEqual(key, "hourly:2026-03-02T14")

    def test_exact_next_hour_enters_new_window(self):
        dt = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)
        key, _, _ = _window_key("hourly", dt)
        self.assertEqual(key, "hourly:2026-03-02T15")


# ══════════════════════════════════════════════════════════════════════════════
# _conditions_match
# ══════════════════════════════════════════════════════════════════════════════

class TestConditionsMatch(unittest.TestCase):

    def test_empty_conditions_always_match(self):
        self.assertTrue(_conditions_match({}, _req()))
        self.assertTrue(_conditions_match(None, _req()))

    def test_provider_match_and_mismatch(self):
        c = {"providers": ["anthropic", "openai"]}
        self.assertTrue(_conditions_match(c, _req(provider="anthropic")))
        self.assertFalse(_conditions_match(c, _req(provider="bedrock")))

    def test_model_pattern_exact(self):
        c = {"model_pattern": "claude-opus-4-6"}
        self.assertTrue(_conditions_match(c, _req(model="claude-opus-4-6")))
        self.assertFalse(_conditions_match(c, _req(model="claude-haiku-4-5")))

    def test_model_pattern_glob(self):
        c = {"model_pattern": "gpt-4*"}
        self.assertTrue(_conditions_match(c, _req(model="gpt-4o")))
        self.assertTrue(_conditions_match(c, _req(model="gpt-4-turbo")))
        self.assertFalse(_conditions_match(c, _req(model="gpt-3.5-turbo")))

    def test_model_pattern_case_insensitive(self):
        c = {"model_pattern": "CLAUDE-OPUS*"}
        self.assertTrue(_conditions_match(c, _req(model="claude-opus-4-6")))

    def test_model_pattern_no_model_fails(self):
        c = {"model_pattern": "gpt-4*"}
        self.assertFalse(_conditions_match(c, _req(model=None)))

    def test_environment_match(self):
        c = {"environments": ["production"]}
        self.assertTrue(_conditions_match(c, _req(environment="production")))
        self.assertFalse(_conditions_match(c, _req(environment="staging")))

    def test_resource_type_match(self):
        c = {"resource_types": ["llm_call"]}
        self.assertTrue(_conditions_match(c, _req(resource_type="llm_call")))
        self.assertFalse(_conditions_match(c, _req(resource_type="embedding")))

    def test_resource_type_none_defaults_to_llm_call(self):
        c = {"resource_types": ["llm_call"]}
        self.assertTrue(_conditions_match(c, _req(resource_type=None)))

    def test_and_logic_all_must_match(self):
        c = {"providers": ["anthropic"], "environments": ["production"],
             "model_pattern": "claude-opus*"}
        ok = _req(provider="anthropic", environment="production", model="claude-opus-4-6")
        self.assertTrue(_conditions_match(c, ok))

        bad_env = _req(provider="anthropic", environment="staging", model="claude-opus-4-6")
        self.assertFalse(_conditions_match(c, bad_env))

        bad_model = _req(provider="anthropic", environment="production", model="claude-haiku-4-5")
        self.assertFalse(_conditions_match(c, bad_model))


# ══════════════════════════════════════════════════════════════════════════════
# _evaluate_policy — all 7 policy types
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluatePolicy(unittest.TestCase):

    # ── model_allowlist ────────────────────────────────────────────────────────

    def test_allowlist_present_model_returns_none(self):
        p = _policy("model_allowlist", config={"models": ["claude-sonnet-4-6"]})
        self.assertIsNone(_run(_evaluate_policy(p, _req(model="claude-sonnet-4-6"), _mock_db(), _now())))

    def test_allowlist_absent_model_denies(self):
        p = _policy("model_allowlist", config={"models": ["claude-haiku-4-5"]})
        r = _run(_evaluate_policy(p, _req(model="claude-opus-4-6"), _mock_db(), _now()))
        self.assertIsNotNone(r)
        self.assertEqual(r.decision, "deny")
        self.assertIn("claude-opus-4-6", r.reason)

    def test_allowlist_no_model_in_request_denies(self):
        p = _policy("model_allowlist", config={"models": ["claude-haiku-4-5"]})
        r = _run(_evaluate_policy(p, _req(model=None), _mock_db(), _now()))
        self.assertIsNotNone(r)

    def test_allowlist_suggested_model_propagated(self):
        p = _policy("model_allowlist", config={"models": ["claude-haiku-4-5"]},
                    action={"suggested_model": "claude-haiku-4-5"})
        r = _run(_evaluate_policy(p, _req(model="claude-opus-4-6"), _mock_db(), _now()))
        self.assertEqual(r.suggested_model, "claude-haiku-4-5")

    # ── model_denylist ─────────────────────────────────────────────────────────

    def test_denylist_blocked_model_denies(self):
        p = _policy("model_denylist", config={"models": ["gpt-4o"]})
        r = _run(_evaluate_policy(p, _req(model="gpt-4o", provider="openai"), _mock_db(), _now()))
        self.assertIsNotNone(r)
        self.assertEqual(r.decision, "deny")

    def test_denylist_unlisted_model_allows(self):
        p = _policy("model_denylist", config={"models": ["gpt-4o"]})
        self.assertIsNone(_run(_evaluate_policy(p, _req(model="gpt-3.5-turbo"), _mock_db(), _now())))

    def test_denylist_no_model_passes(self):
        p = _policy("model_denylist", config={"models": ["gpt-4o"]})
        self.assertIsNone(_run(_evaluate_policy(p, _req(model=None), _mock_db(), _now())))

    # ── provider_block ─────────────────────────────────────────────────────────

    def test_provider_block_blocks_listed(self):
        p = _policy("provider_block", config={"providers": ["openai"]})
        r = _run(_evaluate_policy(p, _req(provider="openai"), _mock_db(), _now()))
        self.assertIsNotNone(r)
        self.assertIn("openai", r.reason)

    def test_provider_block_allows_unlisted(self):
        p = _policy("provider_block", config={"providers": ["openai"]})
        self.assertIsNone(_run(_evaluate_policy(p, _req(provider="anthropic"), _mock_db(), _now())))

    def test_provider_block_empty_list_blocks_nothing(self):
        p = _policy("provider_block", config={"providers": []})
        self.assertIsNone(_run(_evaluate_policy(p, _req(), _mock_db(), _now())))

    # ── environment_block ──────────────────────────────────────────────────────

    def test_environment_block_blocks_staging(self):
        p = _policy("environment_block", config={"environments": ["staging", "dev"]})
        r = _run(_evaluate_policy(p, _req(environment="staging"), _mock_db(), _now()))
        self.assertIsNotNone(r)
        self.assertIn("staging", r.reason)

    def test_environment_block_allows_production(self):
        p = _policy("environment_block", config={"environments": ["staging", "dev"]})
        self.assertIsNone(_run(_evaluate_policy(p, _req(environment="production"), _mock_db(), _now())))

    def test_environment_block_none_environment_passes(self):
        p = _policy("environment_block", config={"environments": ["staging"]})
        self.assertIsNone(_run(_evaluate_policy(p, _req(environment=None), _mock_db(), _now())))

    # ── token_cap ──────────────────────────────────────────────────────────────

    def test_token_cap_under_limit_allows(self):
        rts = FakeRTS(_uuid(), "daily", "d", input_tokens=800, output_tokens=200)
        p = _policy("token_cap", config={"max_tokens": 10000, "period": "daily"})
        self.assertIsNone(_run(_evaluate_policy(p, _req(estimated_tokens=500), _mock_db(rts), _now())))

    def test_token_cap_over_limit_denies(self):
        rts = FakeRTS(_uuid(), "daily", "d", input_tokens=7000, output_tokens=2500)
        p = _policy("token_cap", config={"max_tokens": 10000, "period": "daily"})
        r = _run(_evaluate_policy(p, _req(estimated_tokens=1000), _mock_db(rts), _now()))
        self.assertIsNotNone(r)
        self.assertEqual(r.decision, "deny")
        self.assertIn("10,000", r.reason)

    def test_token_cap_no_rts_row_allows_small_request(self):
        p = _policy("token_cap", config={"max_tokens": 100, "period": "daily"})
        self.assertIsNone(_run(_evaluate_policy(p, _req(estimated_tokens=50), _mock_db(None), _now())))

    def test_token_cap_no_estimate_skips(self):
        p = _policy("token_cap", config={"max_tokens": 1, "period": "daily"})
        self.assertIsNone(_run(_evaluate_policy(p, _req(estimated_tokens=None), _mock_db(), _now())))

    def test_token_cap_zero_max_skips(self):
        p = _policy("token_cap", config={"max_tokens": 0, "period": "daily"})
        self.assertIsNone(_run(_evaluate_policy(p, _req(), _mock_db(), _now())))

    # ── budget_cap ─────────────────────────────────────────────────────────────

    def test_budget_cap_under_limit_allows(self):
        rts = FakeRTS(_uuid(), "daily", "d", total_cost=Decimal("5.00"))
        p = _policy("budget_cap", config={"cap_usd": "10.00", "period": "daily"})
        self.assertIsNone(_run(_evaluate_policy(p, _req(estimated_cost=Decimal("1.00")), _mock_db(rts), _now())))

    def test_budget_cap_current_exceeded_denies(self):
        rts = FakeRTS(_uuid(), "daily", "d", total_cost=Decimal("10.50"))
        p = _policy("budget_cap", config={"cap_usd": "10.00", "period": "daily"})
        r = _run(_evaluate_policy(p, _req(estimated_cost=Decimal("0")), _mock_db(rts), _now()))
        self.assertIsNotNone(r)
        self.assertEqual(r.decision, "deny")
        self.assertIn("$10.00", r.reason)

    def test_budget_cap_projected_over_limit_denies(self):
        rts = FakeRTS(_uuid(), "daily", "d", total_cost=Decimal("9.50"))
        p = _policy("budget_cap", config={"cap_usd": "10.00", "period": "daily"})
        r = _run(_evaluate_policy(p, _req(estimated_cost=Decimal("1.00")), _mock_db(rts), _now()))
        self.assertIsNotNone(r)

    def test_budget_cap_no_rts_row_allows(self):
        p = _policy("budget_cap", config={"cap_usd": "10.00", "period": "daily"})
        self.assertIsNone(_run(_evaluate_policy(p, _req(estimated_cost=Decimal("1.00")), _mock_db(None), _now())))

    def test_budget_cap_zero_cap_skips(self):
        p = _policy("budget_cap", config={"cap_usd": "0", "period": "daily"})
        self.assertIsNone(_run(_evaluate_policy(p, _req(), _mock_db(), _now())))

    def test_budget_cap_throttle_effect_propagates(self):
        rts = FakeRTS(_uuid(), "daily", "d", total_cost=Decimal("10.50"))
        p = _policy("budget_cap", effect="throttle",
                    config={"cap_usd": "10.00", "period": "daily"},
                    action={"retry_after_seconds": 3600})
        r = _run(_evaluate_policy(p, _req(estimated_cost=Decimal("0")), _mock_db(rts), _now()))
        self.assertEqual(r.decision, "throttle")
        self.assertEqual(r.retry_after_seconds, 3600)

    def test_budget_cap_spend_in_reason(self):
        rts = FakeRTS(_uuid(), "daily", "d", total_cost=Decimal("10.50"))
        p = _policy("budget_cap", config={"cap_usd": "10.00", "period": "daily"})
        r = _run(_evaluate_policy(p, _req(estimated_cost=Decimal("0")), _mock_db(rts), _now()))
        self.assertIn("10.50", r.reason)

    # ── rate_limit ─────────────────────────────────────────────────────────────

    def test_rate_limit_under_limit_allows(self):
        rts = FakeRTS(_uuid(), "hourly", "h", call_count=50)
        p = _policy("rate_limit", config={"max_calls": 100, "window_seconds": 3600})
        self.assertIsNone(_run(_evaluate_policy(p, _req(), _mock_db(rts), _now())))

    def test_rate_limit_at_limit_denies(self):
        rts = FakeRTS(_uuid(), "hourly", "h", call_count=100)
        p = _policy("rate_limit", config={"max_calls": 100, "window_seconds": 3600})
        r = _run(_evaluate_policy(p, _req(), _mock_db(rts), _now()))
        self.assertIsNotNone(r)
        self.assertEqual(r.decision, "deny")

    def test_rate_limit_zero_max_skips(self):
        p = _policy("rate_limit", config={"max_calls": 0, "window_seconds": 3600})
        self.assertIsNone(_run(_evaluate_policy(p, _req(), _mock_db(), _now())))

    def test_rate_limit_no_rts_row_starts_from_zero(self):
        p = _policy("rate_limit", config={"max_calls": 10, "window_seconds": 3600})
        self.assertIsNone(_run(_evaluate_policy(p, _req(), _mock_db(None), _now())))

    # ── warn effect ────────────────────────────────────────────────────────────

    def test_warn_effect_returns_result_with_warn_decision(self):
        p = _policy("provider_block", effect="warn", config={"providers": ["openai"]})
        r = _run(_evaluate_policy(p, _req(provider="openai"), _mock_db(), _now()))
        self.assertIsNotNone(r)
        self.assertEqual(r.decision, "warn")

    # ── conditions filter ──────────────────────────────────────────────────────

    def test_conditions_not_matching_returns_none(self):
        p = _policy("provider_block",
                    conditions={"providers": ["openai"]},
                    config={"providers": ["openai"]})
        self.assertIsNone(_run(_evaluate_policy(p, _req(provider="anthropic"), _mock_db(), _now())))

    # ── unknown policy type ────────────────────────────────────────────────────

    def test_unknown_type_skips_without_crashing(self):
        p = _policy("invented_type_xyz", config={})
        self.assertIsNone(_run(_evaluate_policy(p, _req(), _mock_db(), _now())))


# ══════════════════════════════════════════════════════════════════════════════
# Session token
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionToken(unittest.TestCase):

    def test_roundtrip(self):
        app, team = _uuid(), _uuid()
        token = gen_token(app, team)
        self.assertTrue(token.startswith(SESSION_TOKEN_PREFIX))
        got_app, got_team = verify_token(token)
        self.assertEqual(got_app, app)
        self.assertEqual(got_team, team)

    def test_expired_raises(self):
        token = gen_token(_uuid(), _uuid(), ttl=-1)
        with self.assertRaises(SessionTokenError) as ctx:
            verify_token(token)
        self.assertIn("expired", str(ctx.exception).lower())

    def test_tampered_signature_raises(self):
        token = gen_token(_uuid(), _uuid())
        parts = token.rsplit(".", 1)
        tampered = parts[0] + "." + parts[1][:4] + "ZZZZ" + parts[1][8:]
        with self.assertRaises(SessionTokenError):
            verify_token(tampered)

    def test_tampered_payload_raises(self):
        token = gen_token(_uuid(), _uuid())
        body = token[len(SESSION_TOKEN_PREFIX):]
        p64, sig = body.split(".", 1)
        corrupted = SESSION_TOKEN_PREFIX + p64[:4] + "AAAA" + p64[8:] + "." + sig
        with self.assertRaises(SessionTokenError):
            verify_token(corrupted)

    def test_wrong_prefix_raises(self):
        with self.assertRaises(SessionTokenError) as ctx:
            verify_token("mds_not_a_session_token")
        self.assertIn("Not a session token", str(ctx.exception))

    def test_malformed_no_dot_raises(self):
        with self.assertRaises(SessionTokenError):
            verify_token(SESSION_TOKEN_PREFIX + "nodothere")

    def test_is_session_token(self):
        self.assertTrue(is_session_token(gen_token(_uuid(), _uuid())))
        self.assertFalse(is_session_token("mds_abc"))
        self.assertFalse(is_session_token(""))

    def test_two_tokens_same_app_both_valid(self):
        app, team = _uuid(), _uuid()
        t1, t2 = gen_token(app, team), gen_token(app, team)
        self.assertNotEqual(t1, t2)
        self.assertEqual(verify_token(t1)[0], app)
        self.assertEqual(verify_token(t2)[0], app)

    def test_fresh_token_is_not_expired(self):
        verify_token(gen_token(_uuid(), _uuid()))  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
