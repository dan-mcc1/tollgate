"""Shared fixtures.

Database tests run against a throwaway `tollgate_test` database that is recreated and
migrated once per test session, so the dev database is never touched and migrations are
exercised on every run. The upstream is always in-process (the mock app, or an
httpx.MockTransport), so no test touches the network.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import redis.exceptions
import uvicorn
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import make_url, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.types import ASGIApp

from mock_upstream import main as mock
from tollgate.auth import generate_key
from tollgate.config import Settings
from tollgate.db.models import ApiKey, Tenant, UsageRecord
from tollgate.health import UpstreamProbe
from tollgate.limits import BudgetGuard, MemoryRateLimiter, RateLimiter
from tollgate.main import app
from tollgate.proxy.passthrough import create_upstream_client
from tollgate.usage import PriceBook

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:password@localhost:5432/tollgate_test"
)
PROVIDER_KEY = "provider-secret-key-1234"
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/0")


async def recreate_database(url: str) -> None:
    target = make_url(url)
    admin = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    await admin.dispose()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    asyncio.run(recreate_database(TEST_DATABASE_URL))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def sessionmaker(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE usage_records, api_keys, tenants CASCADE"))
    await engine.dispose()


@dataclass
class Keys:
    live: str
    second_live: str
    revoked: str


@pytest.fixture
async def keys(sessionmaker: async_sessionmaker[AsyncSession]) -> Keys:
    """A tenant with two live keys and one revoked key."""
    live, second, revoked = generate_key(), generate_key(), generate_key()
    async with sessionmaker() as session:
        # Far above anything a test sends, so only the tests that mean to be rate
        # limited ever are. Those set the tenant's limit themselves.
        tenant = Tenant(name="acme", rate_limit_rpm=100_000, rate_limit_burst=100_000)
        session.add(tenant)
        await session.flush()
        session.add_all(
            [
                ApiKey(tenant_id=tenant.id, name="a", key_prefix=live.prefix, key_hash=live.hash),
                ApiKey(
                    tenant_id=tenant.id, name="b", key_prefix=second.prefix, key_hash=second.hash
                ),
                ApiKey(
                    tenant_id=tenant.id,
                    name="old",
                    key_prefix=revoked.prefix,
                    key_hash=revoked.hash,
                    revoked_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()
    return Keys(live.plaintext, second.plaintext, revoked.plaintext)


def make_settings() -> Settings:
    return Settings(
        upstream_base_url="http://upstream",
        gemini_api_key=SecretStr(PROVIDER_KEY),
        upstream_backoff_base_s=0,  # retry immediately, keeps tests fast
    )


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def rate_limiter() -> RateLimiter:
    """The limiter behind the gateway fixtures. Override to test the Redis one."""
    return MemoryRateLimiter()


@pytest.fixture
def budget_redis() -> Redis | None:
    """The Redis behind the budget guard. None means it reads the ledger directly,
    which is the default here so most tests need no Redis at all."""
    return None


@pytest.fixture(scope="session")
def redis_url() -> str | None:
    """The test Redis, if one is running, probed once for the whole session.

    Once, because a developer without Redis would otherwise pay a connection timeout
    on every test that touches it rather than one.
    """
    client = redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1)
    try:
        client.ping()
    except (redis.exceptions.RedisError, OSError):
        return None
    finally:
        client.close()
    return TEST_REDIS_URL


@pytest.fixture
async def maybe_redis(redis_url: str | None) -> AsyncIterator[Redis | None]:
    """A flushed Redis, or None when there is not one to be had."""
    if redis_url is None:
        yield None
        return
    client: Redis = Redis.from_url(redis_url)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def redis_client(maybe_redis: Redis | None) -> Redis:
    """As above, for a test that has no meaning without Redis."""
    if maybe_redis is None:
        pytest.skip(f"no Redis at {TEST_REDIS_URL}")
    return maybe_redis


@pytest.fixture
def upstream_transport() -> httpx.AsyncBaseTransport | None:
    """The default upstream: the mock Gemini app, in-process. Override per test.

    None means "use the network", which with a loopback base URL reaches a local server.
    """
    return httpx.ASGITransport(app=mock.app)


@asynccontextmanager
async def serve(application: ASGIApp) -> AsyncIterator[str]:
    """Run an ASGI app on a loopback port for the duration of the block; yield its URL."""
    config = uvicorn.Config(
        application, host="127.0.0.1", port=0, lifespan="off", log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        # Port 0 means the OS picks one, so wait until the server is bound before reading
        # it back. Polling because uvicorn exposes a flag, not an awaitable event.
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        await serving


@pytest.fixture
async def live_upstream() -> AsyncIterator[str]:
    """The mock Gemini API as a real server, for tests that need it to actually stream."""
    async with serve(mock.app) as url:
        yield url


@pytest.fixture
def mock_upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(mock, "LATENCY_MS", 0)
    mock.stats.reset()
    yield
    mock.stats.reset()


@pytest.fixture
async def gateway(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    upstream_transport: httpx.AsyncBaseTransport,
    mock_upstream: None,
    rate_limiter: RateLimiter,
    budget_redis: "Redis | None",
) -> AsyncIterator[httpx.AsyncClient]:
    """A client for the gateway, wired the way lifespan wires it, but to test resources."""
    upstream = create_upstream_client(settings, transport=upstream_transport)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.http_client = upstream
    app.state.upstream_probe = UpstreamProbe(cache_s=0)
    # Prices come from the migration that seeds them, so tests price the same way
    # production does. refresh_s=0 means a price a test inserts is visible at once.
    app.state.pricebook = PriceBook(sessionmaker, refresh_s=0)
    app.state.rate_limiter = rate_limiter
    app.state.budget = BudgetGuard(
        sessionmaker,
        app.state.pricebook,
        budget_redis,
        lease_s=300.0,
        default_max_output_tokens=8192,
        month_ttl_s=40 * 24 * 60 * 60,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tollgate"
    ) as client:
        yield client
    await upstream.aclose()


@pytest.fixture
async def live_gateway(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    upstream_transport: httpx.AsyncBaseTransport,
    mock_upstream: None,
    rate_limiter: RateLimiter,
    budget_redis: "Redis | None",
) -> AsyncIterator[httpx.AsyncClient]:
    """The gateway behind a real HTTP server on a loopback port.

    Streaming can't be tested through httpx's in-process ASGI transport: it collects the
    whole response before handing it back, so events never arrive one at a time and a
    client can never hang up mid-response. A real server on 127.0.0.1 behaves like the
    load balancer will, and still touches no network.
    """
    upstream = create_upstream_client(settings, transport=upstream_transport)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.http_client = upstream
    app.state.upstream_probe = UpstreamProbe(cache_s=0)
    # Prices come from the migration that seeds them, so tests price the same way
    # production does. refresh_s=0 means a price a test inserts is visible at once.
    app.state.pricebook = PriceBook(sessionmaker, refresh_s=0)
    app.state.rate_limiter = rate_limiter
    app.state.budget = BudgetGuard(
        sessionmaker,
        app.state.pricebook,
        budget_redis,
        lease_s=300.0,
        default_max_output_tokens=8192,
        month_ttl_s=40 * 24 * 60 * 60,
    )

    async with serve(app) as url, httpx.AsyncClient(base_url=url, timeout=30) as client:
        yield client
    await upstream.aclose()


async def usage_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[UsageRecord]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(UsageRecord))).all())
