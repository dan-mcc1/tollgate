"""The admin commands.

These run against a session built the way `cli.run` builds one - `async_sessionmaker`
with its defaults - rather than the `expire_on_commit=False` session the gateway uses.
That difference is the point: a commit expires every loaded instance, so a command that
reads an attribute afterwards triggers a lazy reload, which on an async engine raises
instead of blocking. The gateway's own fixtures cannot catch that class of bug.
"""

import argparse
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import TEST_DATABASE_URL
from tollgate.cli import (
    create_key,
    create_tenant,
    list_prices,
    list_tenants,
    parse_usd,
    revoke_key,
    set_limits,
    set_price,
)
from tollgate.db.models import ApiKey, ModelPrice, Tenant

SEEDED_PRICES_AT = datetime(2026, 1, 1, tzinfo=UTC)
Sessionmaker = async_sessionmaker[AsyncSession]


@pytest.fixture
async def cli_session(sessionmaker: Sessionmaker) -> AsyncIterator[AsyncSession]:
    """A session with the library defaults, exactly as the CLI makes one.

    It depends on conftest's `sessionmaker` only for that fixture's teardown, which
    truncates the tenant tables after every test.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with async_sessionmaker(engine)() as session:
        yield session
        # The truncate must leave the seeded prices alone, so versions a test added
        # are cleaned up here instead.
        await session.execute(
            delete(ModelPrice).where(ModelPrice.effective_from > SEEDED_PRICES_AT)
        )
        await session.commit()
    await engine.dispose()


def args(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


# --- tenants and limits -----------------------------------------------------------------


async def test_a_new_tenant_gets_a_limit_rather_than_none(cli_session: AsyncSession) -> None:
    """Limits are opt-out, not opt-in: a tenant created with no arguments is capped."""
    await create_tenant(cli_session, "capped")

    tenant = await cli_session.scalar(select(Tenant).where(Tenant.name == "capped"))

    assert tenant is not None
    assert tenant.rate_limit_rpm == 60


async def test_set_limits_reports_the_new_values(
    cli_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_tenant(cli_session, "acme")

    await set_limits(cli_session, args(tenant="acme", rpm=120, burst=240, unlimited=False))

    assert "acme: 120 rpm, burst 240" in capsys.readouterr().out


async def test_set_limits_can_exempt_a_tenant(
    cli_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_tenant(cli_session, "internal")

    await set_limits(cli_session, args(tenant="internal", rpm=None, burst=None, unlimited=True))

    tenant = await cli_session.scalar(select(Tenant).where(Tenant.name == "internal"))
    assert tenant is not None and tenant.rate_limit_rpm is None
    assert "internal: unlimited" in capsys.readouterr().out


async def test_set_limits_leaves_out_what_was_not_given(cli_session: AsyncSession) -> None:
    await create_tenant(cli_session, "partial")

    await set_limits(cli_session, args(tenant="partial", rpm=None, burst=500, unlimited=False))

    tenant = await cli_session.scalar(select(Tenant).where(Tenant.name == "partial"))
    assert tenant is not None
    assert (tenant.rate_limit_rpm, tenant.rate_limit_burst) == (60, 500)


async def test_an_unknown_tenant_is_refused(cli_session: AsyncSession) -> None:
    with pytest.raises(SystemExit):
        await set_limits(cli_session, args(tenant="nobody", rpm=1, burst=1, unlimited=False))


async def test_tenants_lists_limits(
    cli_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_tenant(cli_session, "acme")
    await set_limits(cli_session, args(tenant="acme", rpm=30, burst=None, unlimited=False))
    capsys.readouterr()

    await list_tenants(cli_session)

    assert "30/min burst 60" in capsys.readouterr().out


# --- keys ---------------------------------------------------------------------------------


async def test_a_key_is_printed_once_and_stored_hashed(
    cli_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_tenant(cli_session, "acme")

    await create_key(cli_session, "acme", "laptop")

    printed = capsys.readouterr().out
    plaintext = next(word for word in printed.split() if word.startswith("tg_"))
    key = await cli_session.scalar(select(ApiKey))
    assert key is not None
    assert plaintext not in key.key_hash
    assert key.key_prefix == plaintext[:12]

    await revoke_key(cli_session, key.key_prefix)

    await cli_session.refresh(key)
    assert key.revoked_at is not None


# --- prices -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usd", "expected"),
    [("0.30", 30_000_000), ("2.50", 250_000_000), ("10", 1_000_000_000), ("0", 0)],
)
def test_dollars_become_exact_micro_cents(usd: str, expected: int) -> None:
    assert parse_usd(usd) == expected


@pytest.mark.parametrize("usd", ["0.000000001", "abc", ""])
def test_a_price_that_cannot_be_represented_is_refused(usd: str) -> None:
    """Refused, not truncated. A price table is the last place to round silently."""
    with pytest.raises(SystemExit):
        parse_usd(usd)


async def test_set_price_adds_a_version_and_leaves_the_old_one(
    cli_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await set_price(
        cli_session,
        args(
            model="gemini-3.7-flash",
            input="0.35",
            output="2.75",
            thoughts=None,
            effective_from="2026-11-01T00:00:00+00:00",
        ),
    )

    versions = list(
        await cli_session.scalars(select(ModelPrice).where(ModelPrice.model == "gemini-3.7-flash"))
    )
    assert len(versions) == 2  # the seeded version is untouched
    added = max(versions, key=lambda row: row.effective_from)
    assert added.input_microcents_per_mtok == 35_000_000
    # Thinking tokens follow the output rate unless a rate is given for them.
    assert added.thoughts_microcents_per_mtok == added.output_microcents_per_mtok
    assert "Priced gemini-3.7-flash" in capsys.readouterr().out


async def test_prices_lists_every_version(
    cli_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await list_prices(cli_session)

    printed = capsys.readouterr().out
    assert "gemini-3.7-flash" in printed
    assert "$0.300000" in printed
