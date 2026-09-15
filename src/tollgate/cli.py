"""Admin commands for tenants and keys.

uv run tollgate create-tenant acme
uv run tollgate create-key acme --name laptop
uv run tollgate list-keys acme
uv run tollgate revoke-key tg_AbCdEfGh
"""

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tollgate.auth import generate_key
from tollgate.config import get_settings
from tollgate.db.models import ApiKey, Tenant


async def create_tenant(session: AsyncSession, name: str) -> None:
    session.add(Tenant(name=name))
    await session.commit()
    print(f"Created tenant {name}")


async def get_tenant(session: AsyncSession, name: str) -> Tenant:
    tenant = await session.scalar(select(Tenant).where(Tenant.name == name))
    if tenant is None:
        raise SystemExit(f"No tenant named {name!r}")
    return tenant


async def create_key(session: AsyncSession, tenant_name: str, key_name: str) -> None:
    tenant = await get_tenant(session, tenant_name)
    key = generate_key()
    session.add(
        ApiKey(tenant_id=tenant.id, name=key_name, key_prefix=key.prefix, key_hash=key.hash)
    )
    await session.commit()
    print(f"Created key {key.prefix}... for {tenant_name}. It is shown once and not stored:\n")
    print(f"  {key.plaintext}\n")


async def list_keys(session: AsyncSession, tenant_name: str) -> None:
    tenant = await get_tenant(session, tenant_name)
    rows = await session.scalars(
        select(ApiKey).where(ApiKey.tenant_id == tenant.id).order_by(ApiKey.created_at)
    )
    for key in rows:
        state = f"revoked {key.revoked_at:%Y-%m-%d}" if key.revoked_at else "live"
        print(f"{key.key_prefix}...  {key.name:<20} {state}")


async def revoke_key(session: AsyncSession, prefix: str) -> None:
    matches = list(await session.scalars(select(ApiKey).where(ApiKey.key_prefix == prefix)))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one key with prefix {prefix!r}, found {len(matches)}")
    matches[0].revoked_at = datetime.now(UTC)
    await session.commit()
    print(f"Revoked {prefix}...")


async def run(args: argparse.Namespace) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            match args.command:
                case "create-tenant":
                    await create_tenant(session, args.name)
                case "create-key":
                    await create_key(session, args.tenant, args.name)
                case "list-keys":
                    await list_keys(session, args.tenant)
                case "revoke-key":
                    await revoke_key(session, args.prefix)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="tollgate")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create-tenant").add_argument("name")
    create = commands.add_parser("create-key")
    create.add_argument("tenant")
    create.add_argument("--name", default="default")
    commands.add_parser("list-keys").add_argument("tenant")
    commands.add_parser("revoke-key").add_argument("prefix", help="the key's first 12 characters")
    asyncio.run(run(parser.parse_args()))
