"""
Modus — Test Fixtures
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add SDK package to sys.path so 'modus' is importable in tests
_sdk_dir = str(Path(__file__).resolve().parent.parent / "sdk")
if _sdk_dir not in sys.path:
    sys.path.insert(0, _sdk_dir)

# Override settings BEFORE importing any orchestrator modules
os.environ.setdefault("MODUS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MODUS_AUTH_MODE", "stub")
os.environ.setdefault("MODUS_ENVIRONMENT", "development")
os.environ.setdefault("CONDUCTOR_ENVIRONMENT", "development")
os.environ.setdefault("FEDERATOR_ENVIRONMENT", "development")
os.environ.setdefault("MODUS_METRICS_ENABLED", "false")
os.environ.setdefault("MODUS_MASTER_KEY_HASH", "")
os.environ.setdefault("MODUS_SECRET_KEY", "test-secret-key-for-testing-only")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orchestrator.db.models import Base
from orchestrator.db import session as session_mod
from orchestrator.db.session import get_session, get_read_session


@pytest.fixture(autouse=True)
def _reset_gateway_circuit():
    """Clear the shared gateway circuit breaker between tests — its per-provider
    failure state is module-global and would otherwise leak across tests."""
    from orchestrator.core.gateway_resilience import get_breaker
    get_breaker().reset()
    yield
    get_breaker().reset()


@pytest.fixture(autouse=True)
def _reset_api_rate_limiters():
    """The API rate limiters are process-global sliding windows keyed by API
    key or client IP. Every ASGI test client shares one IP, so without a reset
    a fast run accumulates >250 requests/minute across test files and later
    tests get 429s depending on execution speed."""
    import sys
    main = sys.modules.get("orchestrator.main")
    if main is not None:
        for name in ("_rate_limiter", "_sdk_rate_limiter"):
            limiter = getattr(main, name, None)
            if limiter is not None:
                with limiter._lock:
                    limiter._counters.clear()
    yield


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(engine):
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

    # Inject test engine so get_engine() works (used by /ready health check)
    _prev_engine = session_mod._engine
    _prev_factory = session_mod._session_factory
    session_mod._engine = engine
    # Initialize the global session factory so handlers using get_read_session
    # (which reads _session_factory directly) work in tests too.
    session_mod._session_factory = factory

    from orchestrator.main import create_app
    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_read_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    session_mod._engine = _prev_engine
    session_mod._session_factory = _prev_factory


@pytest_asyncio.fixture
async def registered_app(db_session):
    """A team and an app with a real, bcrypt-hashed stable API key.

    Returns a dict with ``app_uuid``, ``team_id`` and the raw ``api_key`` so
    tests can call SDK endpoints through real key verification.
    """
    from orchestrator.api.apps import _generate_app_key, _hash_key
    from orchestrator.db.models import App, Team

    team = Team(slug="routing-team", name="Routing Team")
    db_session.add(team)
    await db_session.flush()

    raw_key = _generate_app_key()
    app = App(
        team_id=team.id,
        app_id="routing-app",
        app_name="Routing App",
        api_key_hash=_hash_key(raw_key, rounds=4),
        api_key_prefix=raw_key[:16],
    )
    db_session.add(app)
    await db_session.commit()
    return {"app_uuid": str(app.id), "team_id": str(team.id), "api_key": raw_key}
