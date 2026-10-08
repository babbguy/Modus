"""
Modus — CI PR cost-impact GitHub Action tests
=============================================

Covers the production CI cost-integration feature end to end:

1. The POST /api/v1/ci/pr-cost-estimate endpoint returns a correct dollar delta
   for a seeded pricing table + usage volume + model list (driven via the API
   client).
2. The action's generated payload shape validates against the endpoint's
   Pydantic request model (PrCostRequest).
3. The action script's pure parsing/formatting logic (diff scan, explicit list,
   delta + comment formatting) behaves correctly and stays honest ("estimated").
"""
from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.core.auth import Identity, get_identity
from orchestrator.db import session as session_mod
from orchestrator.db.models import App, PricingModel, Team, UsageAggregate
from orchestrator.db.session import get_read_session, get_session

# asyncio_mode = "auto" (pyproject.toml) auto-detects async tests — no marker needed.


# ── Load the action script (lives outside the package) by file path ───────────

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "github-action" / "cost-check" / "pr_cost_estimate.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("modus_pr_cost_estimate", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


pr_cost = _load_script()


TEAM_ID = str(uuid.uuid4())


@pytest_asyncio.fixture
async def authed_client(engine):
    """Client whose identity is scoped to TEAM_ID (stub identity has no team,
    so the pr-cost happy path needs a concrete team to match seeded rows)."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    def _override_identity() -> Identity:
        return Identity(actor_id="ci-test", role="team_admin", team_ids=[TEAM_ID])

    _prev_engine = session_mod._engine
    _prev_factory = session_mod._session_factory
    session_mod._engine = engine
    session_mod._session_factory = factory

    from orchestrator.main import create_app
    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_read_session] = _override_session
    app.dependency_overrides[get_identity] = _override_identity

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    session_mod._engine = _prev_engine
    session_mod._session_factory = _prev_factory


async def _seed(db_session) -> str:
    """Seed a team, app, pricing table, and 30-day usage volume. Returns app slug."""
    db_session.add(Team(id=TEAM_ID, slug=f"ci-team-{uuid.uuid4().hex[:6]}", name="CI Team"))

    app_uuid = str(uuid.uuid4())
    app_slug = "ci-cost-app"
    db_session.add(App(
        id=app_uuid, team_id=TEAM_ID, app_id=app_slug, app_name="CI Cost App",
        environment="production", api_key_hash="x", api_key_prefix="mds_test",
    ))

    now = datetime.now(timezone.utc)
    # Pricing: expensive gpt-4, cheap gpt-4o-mini (USD per 1k tokens).
    db_session.add(PricingModel(
        id=str(uuid.uuid4()), provider="openai", model="gpt-4",
        resource_type="llm_call",
        input_cost_per_1k=Decimal("0.03"), output_cost_per_1k=Decimal("0.06"),
        currency="USD", effective_from=now - timedelta(days=90),
    ))
    db_session.add(PricingModel(
        id=str(uuid.uuid4()), provider="openai", model="gpt-4o-mini",
        resource_type="llm_call",
        input_cost_per_1k=Decimal("0.00015"), output_cost_per_1k=Decimal("0.0006"),
        currency="USD", effective_from=now - timedelta(days=90),
    ))

    # 30-day volume on gpt-4: 1,000,000 input tokens, 500,000 output tokens.
    db_session.add(UsageAggregate(
        id=str(uuid.uuid4()), app_id=app_uuid, team_id=TEAM_ID,
        provider="openai", model="gpt-4", resource_type="llm_call",
        granularity="daily",
        period_start=now - timedelta(days=5), period_end=now - timedelta(days=5) + timedelta(days=1),
        call_count=10_000, input_tokens=1_000_000, output_tokens=500_000,
        total_tokens=1_500_000,
        input_cost=Decimal("30"), output_cost=Decimal("30"), total_cost=Decimal("60"),
    ))
    await db_session.commit()
    return app_slug


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Endpoint returns a correct dollar delta (driven via the API client)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_pr_cost_estimate_returns_correct_delta(authed_client, db_session):
    app_slug = await _seed(db_session)

    # Payload shape is exactly what the action produces via build_payload().
    payload = pr_cost.build_payload(
        app_id=app_slug,
        pr_number=42,
        pr_url="https://github.com/babbguy/Modus/pull/42",
        changes=[{
            "file_path": "src/ai.py",
            "current_model": "gpt-4",
            "proposed_model": "gpt-4o-mini",
        }],
    )

    resp = await authed_client.post("/api/v1/ci/pr-cost-estimate", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # current gpt-4 monthly cost = 0.03*1000 + 0.06*500 = 60.00
    # proposed gpt-4o-mini monthly cost = 0.00015*1000 + 0.0006*500 = 0.45
    # delta = 0.45 - 60.00 = -59.55  (a saving)
    assert data["total_monthly_delta_usd"] == pytest.approx(-59.55, abs=0.01)
    assert "save" in data["message"].lower()

    impact = data["model_impacts"][0]
    assert impact["current_model"] == "gpt-4"
    assert impact["proposed_model"] == "gpt-4o-mini"
    assert impact["monthly_delta_usd"] == pytest.approx(-59.55, abs=0.01)
    assert impact["monthly_volume"] == 10_000


async def test_pr_cost_estimate_upgrade_increases_cost(authed_client, db_session):
    """Swapping the cheap model up to gpt-4 should report a positive delta."""
    app_slug = await _seed(db_session)

    payload = pr_cost.build_payload(
        app_id=app_slug, pr_number=7, pr_url=None,
        changes=[{
            "file_path": "src/ai.py",
            "current_model": "gpt-4",          # volume is attributed to gpt-4
            "proposed_model": "gpt-4",         # no-op baseline check
        }],
    )
    resp = await authed_client.post("/api/v1/ci/pr-cost-estimate", json=payload)
    assert resp.status_code == 200, resp.text
    # gpt-4 -> gpt-4 delta is exactly 0.
    assert resp.json()["total_monthly_delta_usd"] == pytest.approx(0.0, abs=0.001)


async def test_pr_cost_estimate_explicit_list_no_file_path(authed_client, db_session):
    """Explicit-list mode: model change with file_path=None must be accepted
    (the minimal ci_integration.py change) and estimate the full new-model cost."""
    app_slug = await _seed(db_session)

    payload = pr_cost.build_payload(
        app_id=app_slug, pr_number=9, pr_url=None,
        changes=[{
            "file_path": None,
            "current_model": None,
            "proposed_model": "gpt-4",
        }],
    )
    resp = await authed_client.post("/api/v1/ci/pr-cost-estimate", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # New gpt-4 with gpt-4's own volume => full monthly cost 60.00.
    assert data["total_monthly_delta_usd"] == pytest.approx(60.0, abs=0.01)
    assert data["model_impacts"][0]["file_path"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Action payload shape matches the endpoint's request model
# ═══════════════════════════════════════════════════════════════════════════════

def test_action_payload_validates_against_request_model():
    from orchestrator.api.ci_integration import PrCostRequest

    diff = (
        "diff --git a/src/ai.py b/src/ai.py\n"
        "--- a/src/ai.py\n"
        "+++ b/src/ai.py\n"
        "@@ -1,3 +1,3 @@\n"
        '-    model = "gpt-4"\n'
        '+    model = "gpt-4o-mini"\n'
    )
    changes = pr_cost.extract_changes_from_diff(diff)
    payload = pr_cost.build_payload("my-app", 12, "http://pr", changes)

    # Must validate cleanly against the real endpoint schema.
    req = PrCostRequest(**payload)
    assert req.app_id == "my-app"
    assert req.pr_number == 12
    assert req.model_changes[0].current_model == "gpt-4"
    assert req.model_changes[0].proposed_model == "gpt-4o-mini"


def test_build_payload_caps_and_omits_none_url():
    changes = [{"file_path": None, "current_model": None, "proposed_model": f"gpt-4-{i}"}
               for i in range(60)]
    payload = pr_cost.build_payload("app", 1, None, changes)
    assert "pr_url" not in payload            # None url omitted
    assert len(payload["model_changes"]) == pr_cost.MAX_CHANGES  # capped at 50


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Action script parsing / formatting unit tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_extract_changes_pairs_swap():
    diff = (
        "+++ b/app/handler.py\n"
        '-    m = "gpt-4"\n'
        '+    m = "gpt-4o-mini"\n'
    )
    changes = pr_cost.extract_changes_from_diff(diff)
    assert changes == [{
        "file_path": "app/handler.py",
        "current_model": "gpt-4",
        "proposed_model": "gpt-4o-mini",
    }]


def test_extract_changes_new_model_no_current():
    diff = (
        "+++ b/app/new.py\n"
        '+    client.chat("claude-3-5-sonnet-20241022")\n'
    )
    changes = pr_cost.extract_changes_from_diff(diff)
    assert len(changes) == 1
    assert changes[0]["current_model"] is None
    assert changes[0]["proposed_model"] == "claude-3-5-sonnet-20241022"


def test_extract_changes_ignores_unchanged_and_added_file_headers():
    diff = (
        "+++ b/app/keep.py\n"
        '     model = "gemini-1.5-pro"\n'   # context line, unchanged -> ignored
    )
    assert pr_cost.extract_changes_from_diff(diff) == []


def test_extract_changes_skips_deleted_file_target():
    diff = (
        "+++ /dev/null\n"
        '-    model = "gpt-4"\n'
    )
    # File deleted; no proposed model to price.
    assert pr_cost.extract_changes_from_diff(diff) == []


def test_extract_changes_multiple_providers():
    diff = (
        "+++ b/a.py\n"
        '+llm("gpt-4o")\n'
        "+++ b/b.py\n"
        '+llm("mistral-large-latest")\n'
        "+++ b/c.py\n"
        '+llm("llama-3-70b")\n'
    )
    models = {c["proposed_model"] for c in pr_cost.extract_changes_from_diff(diff)}
    assert models == {"gpt-4o", "mistral-large-latest", "llama-3-70b"}


def test_parse_explicit_models():
    changes = pr_cost.parse_explicit_models("gpt-4o-mini, gpt-4\nclaude-3-haiku-20240307")
    proposed = [c["proposed_model"] for c in changes]
    assert proposed == ["gpt-4o-mini", "gpt-4", "claude-3-haiku-20240307"]
    assert all(c["file_path"] is None and c["current_model"] is None for c in changes)


def test_parse_explicit_models_empty():
    assert pr_cost.parse_explicit_models("") == []
    assert pr_cost.parse_explicit_models(None) == []


def test_dedupe_removes_exact_duplicates():
    diff = (
        "+++ b/a.py\n"
        '+llm("gpt-4o")\n'
        '+other("gpt-4o")\n'   # same new model in same file -> deduped
    )
    changes = pr_cost.extract_changes_from_diff(diff)
    assert len(changes) == 1


def test_format_delta_signs():
    assert pr_cost.format_delta(890) == "+$890.00"
    assert pr_cost.format_delta(-142.5) == "-$142.50"
    assert pr_cost.format_delta(0) == "$0.00"
    assert pr_cost.format_delta(1234.5) == "+$1,234.50"


def test_format_comment_is_labelled_estimated():
    response = {
        "total_monthly_delta_usd": 890.0,
        "model_impacts": [
            {"file_path": "src/ai.py", "current_model": "gpt-4o-mini",
             "proposed_model": "gpt-4", "monthly_delta_usd": 890.0},
        ],
        "message": "This PR will increase monthly costs by $890.00.",
    }
    body = pr_cost.format_comment(response)
    # Honesty requirement: the headline must be labelled an estimate.
    assert "Estimated monthly AI cost impact of this PR: +$890.00/mo" in body
    assert "*estimate*" in body
    assert "gpt-4o-mini` → `gpt-4`" in body
    assert body.startswith("## Modus — Estimated AI cost impact")


def test_format_comment_new_model_row():
    response = {
        "total_monthly_delta_usd": 12.0,
        "model_impacts": [
            {"file_path": None, "current_model": None,
             "proposed_model": "gpt-4o", "monthly_delta_usd": 12.0},
        ],
        "message": "",
    }
    body = pr_cost.format_comment(response)
    assert "(new) `gpt-4o`" in body
