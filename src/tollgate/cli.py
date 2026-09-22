"""Admin commands for tenants, keys and prices.

uv run tollgate create-tenant acme
uv run tollgate create-key acme --name laptop
uv run tollgate list-keys acme
uv run tollgate revoke-key tg_AbCdEfGh
uv run tollgate tenants
uv run tollgate set-limits acme --rpm 120 --burst 240
uv run tollgate set-budget acme --usd 25
    uv run tollgate set-detection acme --mode block
uv run tollgate prices
uv run tollgate set-price gemini-3.7-flash --input 0.30 --output 2.50
uv run tollgate cache
uv run tollgate cache-prune
uv run tollgate cache-clear acme
"""

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tollgate.auth import generate_key
from tollgate.cache import exact
from tollgate.config import get_settings
from tollgate.db.models import ApiKey, ModelPrice, Tenant
from tollgate.detect.service import MODES
from tollgate.usage import MICROCENTS_PER_USD, format_usd


async def create_tenant(session: AsyncSession, name: str) -> None:
    session.add(Tenant(name=name))
    await session.commit()
    print(f"Created tenant {name}")


async def get_tenant(session: AsyncSession, name: str) -> Tenant:
    tenant = await session.scalar(select(Tenant).where(Tenant.name == name))
    if tenant is None:
        raise SystemExit(f"No tenant named {name!r}")
    return tenant


async def list_tenants(session: AsyncSession) -> None:
    rows = await session.scalars(select(Tenant).order_by(Tenant.name))
    for tenant in rows:
        rpm, burst = tenant.rate_limit_rpm, tenant.rate_limit_burst
        limit = "unlimited" if rpm is None else f"{rpm}/min burst {burst or rpm}"
        cap = tenant.monthly_budget_microcents
        budget = "no budget" if cap is None else f"{format_usd(cap)}/month"
        state = "active" if tenant.is_active else "inactive"
        mode = f"detect {tenant.detection_mode}"
        print(f"{tenant.name:<24} {state:<10} {limit:<24} {budget:<20} {mode}")


async def set_limits(session: AsyncSession, args: argparse.Namespace) -> None:
    tenant = await get_tenant(session, args.tenant)
    if args.unlimited:
        tenant.rate_limit_rpm = tenant.rate_limit_burst = None
    else:
        if args.rpm is not None:
            tenant.rate_limit_rpm = args.rpm
        if args.burst is not None:
            tenant.rate_limit_burst = args.burst
    # Read before committing. A commit expires the instance, so touching an attribute
    # afterwards is a lazy reload - which raises rather than blocks on an async engine.
    rpm, burst = tenant.rate_limit_rpm, tenant.rate_limit_burst
    await session.commit()
    shown = "unlimited" if rpm is None else f"{rpm} rpm, burst {burst or rpm}"
    print(f"{args.tenant}: {shown}")


async def set_budget(session: AsyncSession, args: argparse.Namespace) -> None:
    tenant = await get_tenant(session, args.tenant)
    tenant.monthly_budget_microcents = None if args.unlimited else parse_usd(args.usd)
    # Read before committing; a commit expires the instance.
    cap = tenant.monthly_budget_microcents
    await session.commit()
    shown = "no budget" if cap is None else f"{format_usd(cap)} per month"
    print(f"{args.tenant}: {shown}")


async def set_detection(session: AsyncSession, args: argparse.Namespace) -> None:
    """Set what happens to a request this tenant's users get flagged for.

    The check constraint on the column would refuse an unknown mode anyway; refusing it
    here means the operator gets a list of the legal ones instead of a database error.
    """
    if args.mode not in MODES:
        raise SystemExit(f"--mode must be one of {', '.join(sorted(MODES))}")
    tenant = await get_tenant(session, args.tenant)
    tenant.detection_mode = args.mode
    await session.commit()
    print(f"{args.tenant}: detection {args.mode}")


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


def parse_usd(usd: str) -> int:
    """Dollars, as typed, to integer micro-cents.

    Decimal and not float, because 0.30 has no exact binary form and neither a price
    table nor a budget is a place to accept a rounding error. An amount finer than a
    micro-cent is refused rather than silently truncated.
    """
    try:
        value = Decimal(usd) * MICROCENTS_PER_USD
    except InvalidOperation:
        raise SystemExit(f"{usd!r} is not an amount in dollars") from None
    if value != value.to_integral_value():
        raise SystemExit(f"{usd} is finer than a micro-cent")
    return int(value)


async def list_prices(session: AsyncSession) -> None:
    rows = await session.scalars(
        select(ModelPrice).order_by(ModelPrice.model, ModelPrice.effective_from.desc())
    )
    for row in rows:
        print(
            f"{row.model:<24} from {row.effective_from:%Y-%m-%d}  "
            f"in {format_usd(row.input_microcents_per_mtok)}  "
            f"out {format_usd(row.output_microcents_per_mtok)}  "
            f"thinking {format_usd(row.thoughts_microcents_per_mtok)}   per Mtok"
        )


async def set_price(session: AsyncSession, args: argparse.Namespace) -> None:
    """Add a price version. Existing versions are never edited, so rows already in the
    ledger keep the price they were charged at."""
    effective_from = (
        datetime.fromisoformat(args.effective_from).astimezone(UTC)
        if args.effective_from
        else datetime.now(UTC)
    )
    output = parse_usd(args.output)
    session.add(
        ModelPrice(
            model=args.model,
            effective_from=effective_from,
            input_microcents_per_mtok=parse_usd(args.input),
            output_microcents_per_mtok=output,
            # Thinking tokens bill at the output rate unless told otherwise.
            thoughts_microcents_per_mtok=(parse_usd(args.thoughts) if args.thoughts else output),
        )
    )
    await session.commit()
    print(f"Priced {args.model} from {effective_from:%Y-%m-%d %H:%M} UTC")


async def show_cache(session: AsyncSession) -> None:
    """What the cache holds, per tenant. Counts only: no prompt or response is printed,
    and none could be - the request is stored as a hash and cannot be read back."""
    rows = await exact.stats(session)
    if not rows:
        print("The cache is empty.")
        return
    print(f"{'tenant':<24} {'entries':>9} {'hits':>8} {'expired':>9}")
    for row in rows:
        print(f"{row.tenant:<24} {row.entries:>9} {row.hits:>8} {row.expired:>9}")


async def prune_cache(session: AsyncSession) -> None:
    removed = await exact.prune(session)
    print(f"Removed {removed} expired {'entry' if removed == 1 else 'entries'}")


async def clear_cache(session: AsyncSession, name: str) -> None:
    tenant = await get_tenant(session, name)
    removed = await exact.clear(session, tenant.id)
    print(f"Removed {removed} {'entry' if removed == 1 else 'entries'} for {name}")


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
                case "tenants":
                    await list_tenants(session)
                case "set-limits":
                    await set_limits(session, args)
                case "set-budget":
                    await set_budget(session, args)
                case "set-detection":
                    await set_detection(session, args)
                case "prices":
                    await list_prices(session)
                case "set-price":
                    await set_price(session, args)
                case "cache":
                    await show_cache(session)
                case "cache-prune":
                    await prune_cache(session)
                case "cache-clear":
                    await clear_cache(session, args.tenant)
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
    commands.add_parser("tenants")
    limits = commands.add_parser("set-limits")
    limits.add_argument("tenant")
    limits.add_argument("--rpm", type=int, help="sustained requests per minute")
    limits.add_argument("--burst", type=int, help="how many may arrive at once")
    limits.add_argument(
        "--unlimited", action="store_true", help="exempt this tenant from rate limiting"
    )
    money = commands.add_parser("set-budget")
    money.add_argument("tenant")
    money.add_argument("--usd", help="monthly spend cap in dollars, e.g. 25.00")
    money.add_argument("--unlimited", action="store_true", help="remove the cap entirely")
    detection = commands.add_parser("set-detection")
    detection.add_argument("tenant")
    detection.add_argument(
        "--mode",
        required=True,
        choices=sorted(MODES),
        help="off (no inspection), monitor (record only) or block (refuse with 403)",
    )
    commands.add_parser("prices")
    price = commands.add_parser("set-price")
    price.add_argument("model")
    price.add_argument("--input", required=True, help="USD per million input tokens, e.g. 0.30")
    price.add_argument("--output", required=True, help="USD per million output tokens")
    price.add_argument("--thoughts", help="USD per million thinking tokens (default: --output)")
    price.add_argument(
        "--from", dest="effective_from", help="ISO timestamp it takes effect (default: now)"
    )
    commands.add_parser("cache", help="what the response cache is holding, per tenant")
    commands.add_parser("cache-prune", help="delete entries that have expired")
    commands.add_parser("cache-clear", help="drop one tenant's entries").add_argument("tenant")
    asyncio.run(run(parser.parse_args()))
