"""Shared fixtures.

Database tests run against a throwaway `tollgate_test` database that is recreated and
migrated once per test session, so the dev database is never touched and migrations are
exercised on every run. The upstream is always in-process (the mock app, or an
httpx.MockTransport), so no test touches the network.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import make_url, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mock_upstream import main as mock
from tollgate.auth import generate_key
from tollgate.config import Settings
from tollgate.db.models import ApiKey, Tenant, UsageRecord
from tollgate.health import UpstreamProbe
from tollgate.main import app
from tollgate.proxy.passthrough import create_upstream_client

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:password@localhost:5432/tollgate_test"
)
PROVIDER_KEY = "provider-secret-key-1234"


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
        tenant = Tenant(name="acme")
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
def upstream_transport() -> httpx.AsyncBaseTransport:
    """The default upstream: the mock Gemini app, in-process. Override per test."""
    return httpx.ASGITransport(app=mock.app)


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
) -> AsyncIterator[httpx.AsyncClient]:
    """A client for the gateway, wired the way lifespan wires it, but to test resources."""
    upstream = create_upstream_client(settings, transport=upstream_transport)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.http_client = upstream
    app.state.upstream_probe = UpstreamProbe(cache_s=0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tollgate"
    ) as client:
        yield client
    await upstream.aclose()


async def usage_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[UsageRecord]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(UsageRecord))).all())
