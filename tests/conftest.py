"""
Pytest fixtures for AsyncGate tests.
"""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Ensure test config is set before importing asyncgate modules.
os.environ.setdefault("ASYNCGATE_ALLOW_INSECURE_DEV", "true")
os.environ.setdefault("ASYNCGATE_ENV", "development")
os.environ.setdefault(
    "ASYNCGATE_DATABASE_URL",
    os.getenv(
        "ASYNCGATE_TEST_DATABASE_URL",
        "postgresql+asyncpg://asyncgate:asyncgate@localhost:5432/asyncgate_test",
    ),
)

from asyncgate.config import settings
from asyncgate.db.base import Base
import asyncgate.db.tables  # noqa: F401

pytest_plugins = ("pytest_asyncio",)


def _ensure_test_database_url(database_url: str) -> None:
    if "test" not in database_url:
        raise RuntimeError(
            "Refusing to run AsyncGate tests against a non-test database. "
            "Set ASYNCGATE_TEST_DATABASE_URL to a dedicated test database."
        )


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def engine():
    """Create a test engine and wire it into asyncgate.db.base."""
    _ensure_test_database_url(settings.database_url)
    engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
    )

    # Override global engine/session factory for dependency injection.
    from asyncgate import db as db_module

    db_module.base.engine = engine
    db_module.base.async_session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - skips in environments without DB
        await engine.dispose()
        pytest.skip(f"AsyncGate test database unavailable: {exc}")

    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    """Provide a clean database session per test."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(session):
    """Async test client with overridden dependencies."""
    from asyncgate.api.deps import AuthContext, get_db_session, verify_api_key
    from asyncgate.main import app
    from asyncgate.middleware.rate_limit import rate_limit_dependency

    async def override_get_db_session():
        yield session

    async def override_verify_api_key():
        return AuthContext(user=None, auth_type="insecure_dev", is_internal=False)

    async def override_rate_limit():
        return None

    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[verify_api_key] = override_verify_api_key
    app.dependency_overrides[rate_limit_dependency] = override_rate_limit

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
