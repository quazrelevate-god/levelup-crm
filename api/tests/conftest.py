"""Shared test fixtures.

Environment defaults are set before `app` is imported, because Settings has no
hardcoded fallbacks — every value must come from the environment.

Postgres comes from a real container: JSONB and expression indexes, which this
product leans on from M2 onward, do not behave the same on SQLite.

Redis is faked. Login rate limiting is the only thing that needs it in M1, and
its behaviour is fixed-window counting with a TTL — a dict with expiry proves
that correctly and keeps the suite runnable without a second container.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://crm:crm@localhost:5432/crm")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test-access-key")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test-secret-key")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("CORS_ORIGINS", '["http://localhost:5173"]')
os.environ.setdefault("JWT_SECRET_KEY", "test-signing-key-at-least-32-characters-long")
# Keep argon2 cheap in tests: the suite hashes a password per fixture, and the
# production cost parameters make that dominate the run time.
os.environ.setdefault("ARGON2_MEMORY_COST_KIB", "8192")
os.environ.setdefault("ARGON2_TIME_COST", "1")
# The per-IP refresh budget defaults to 120 so a NAT'd office is not throttled;
# proving it trips would mean 121 requests. Lowered here only. The per-token
# budget is left at its production value, because 11 requests is already cheap
# and testing the real number is worth more than testing a stand-in.
# Each test gets its own FakeRedis, so counters never leak between tests.
os.environ.setdefault("REFRESH_RATE_LIMIT_IP_ATTEMPTS", "20")
# The suite places calls at whatever hour it happens to run. Calling-hours
# enforcement has its own tests (test_voice_calling_hours.py), which switch it
# back on with a fixed clock.
os.environ.setdefault("VOICE_CALLING_HOURS", "")

# Credentials that reach real services are *forced* empty, not defaulted:
# pydantic-settings also reads `api/.env`, and a developer's real keys there
# must never reach a test. On 2026-10-24 a real Bolna key in `.env` let one
# test place a real call to a fixture phone number. Environment variables win
# over the `.env` file, so an empty value here means "not configured".
os.environ["BOLNA_API_KEY"] = ""
os.environ["BOLNA_AGENT_ID"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["SENTRY_DSN"] = ""

from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer
from tests.fakes import FakeRedis

from app.auth.passwords import PasswordHasherService
from app.config import Settings, get_settings
from app.db import Base
from app.dependencies import get_session
from app.integrations.bolna import BolnaCallRequest, HttpxBolnaClient
from app.main import create_app

# The second layer: even if a real key ever arrived some other way, the real
# Bolna client refuses to dial from the test suite. Tests use
# `RecordingBolnaClient`; the few that exercise the real client's failure path
# point it at a `.invalid` host, which can never resolve.
_real_place_call = HttpxBolnaClient.place_call


async def _never_dial_from_tests(self: HttpxBolnaClient, request: BolnaCallRequest) -> object:
    host = urlparse(self._base_url).hostname or ""
    if not host.endswith(".invalid"):
        raise AssertionError(
            f"A test tried to place a real Bolna call via {host!r}. "
            "Install RecordingBolnaClient on app.state.bolna_client instead."
        )
    return await _real_place_call(self, request)


HttpxBolnaClient.place_call = _never_dial_from_tests  # type: ignore[method-assign]

POSTGRES_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer | None]:
    """A throwaway Postgres 16, shared by the whole test session.

    Skipped entirely when `TEST_DATABASE_URL` points at an existing server —
    see `postgres_async_dsn`.
    """
    if os.environ.get("TEST_DATABASE_URL"):
        yield None
        return
    with PostgresContainer(POSTGRES_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def postgres_async_dsn(postgres_container: PostgresContainer | None) -> str:
    """asyncpg DSN for the test database.

    `TEST_DATABASE_URL` wins when set, which lets a developer run against a
    local server without Docker. CI leaves it unset and gets the pinned
    Postgres 16 container — the version the product targets, and the one whose
    JSONB and expression-index behaviour M2 onward depends on.

    The container DSN is built by hand rather than via `get_connection_url`,
    whose readiness probe uses a synchronous driver.
    """
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        return external

    assert postgres_container is not None
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return (
        f"postgresql+asyncpg://{postgres_container.username}:"
        f"{postgres_container.password}@{host}:{port}/{postgres_container.dbname}"
    )


@pytest.fixture
async def engine(postgres_async_dsn: str) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(postgres_async_dsn, poolclass=None)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
async def schema_engine(postgres_async_dsn: str) -> AsyncIterator[AsyncEngine]:
    """An engine with the full schema created and dropped around each test.

    `create_all` rather than `alembic upgrade head`: the migration is verified
    separately in `test_migrations.py`, and rebuilding from metadata keeps each
    test independent without a per-test container.
    """
    created = create_async_engine(postgres_async_dsn)
    async with created.begin() as connection:
        await connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS citext")
        await connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield created
    finally:
        async with created.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.exec_driver_sql("DROP TYPE IF EXISTS availability_status")
        await created.dispose()


@pytest.fixture
def session_factory(schema_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=schema_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A plain session for arranging fixtures directly."""
    async with session_factory() as session:
        yield session


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """The application without its lifespan.

    ASGITransport does not run lifespan events, so no real client is built —
    tests inject exactly the dependencies they need.
    """
    return create_app(settings)


@pytest.fixture
def wired_app(
    app: FastAPI,
    settings: Settings,
    schema_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    """The application with a live database, a fake Redis and a hasher.

    The session override yields *one* session per request, matching production,
    so the workspace scoping applied to it in a request is the same scoping the
    handler's later queries run under.
    """
    app.state.redis = FakeRedis()
    app.state.password_hasher = PasswordHasherService(settings)
    app.state.session_factory = session_factory
    # Background tasks reach for the engine directly rather than a session: the
    # indexed-field worker runs `CREATE INDEX CONCURRENTLY`, which needs its own
    # AUTOCOMMIT connection. The lifespan sets this in production.
    app.state.engine = schema_engine

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
async def api(wired_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client against the fully wired application."""
    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def make_operator(wired_app: FastAPI) -> Callable[..., None]:
    """Name the platform operator for one test (docs/16).

    Nobody is the operator by default, exactly as in a deployment that forgot
    to set `PLATFORM_OPERATOR_EMAILS`. The app fixture is per test, so the
    change cannot leak into the next one.
    """

    def _make(*emails: str) -> None:
        wired_app.state.settings = wired_app.state.settings.model_copy(
            update={"platform_operator_emails": list(emails)}
        )

    return _make
