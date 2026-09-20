"""Phase 4: how far a streamed request's budget reservation is from what it really cost.

A budget has to be enforced before the upstream is called, and a streamed response's cost
is not known until it ends. The gateway therefore reserves an estimate built from the
request's own ceiling and settles it afterwards. Two numbers matter, and they pull in
opposite directions:

  * how much the estimate overshoots, which is budget a tenant cannot spend while the
    request is in flight, and
  * the error left after settlement, which is what the tenant is actually billed and
    must be nothing at all.

    docker compose up -d --wait
    uv run tollgate set-budget acme --usd 25
    uv run uvicorn tollgate.main:app --port 8000        # in another terminal
    uv run python bench/reservation_error.py tg_your_tenant_key

The gateway needs REDIS_URL set for the reconciliation half; without it the estimate
half still reports.
"""

import argparse
import asyncio
import json
import statistics
from datetime import UTC, datetime

import httpx
import redis.exceptions
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tollgate.auth import hash_key
from tollgate.config import get_settings
from tollgate.db.models import ApiKey, Tenant, UsageRecord
from tollgate.limits import BudgetGuard, estimate_request
from tollgate.usage import PriceBook, format_usd, month_bounds

MODEL = "gemini-3.7-flash"
PATH = f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
# A spread, because the overshoot is entirely a function of how close the ceiling a
# request asks for is to the length it actually runs to.
CEILINGS = [64, 128, 256, 512, 1024, 2048, 4096]

Sessionmaker = async_sessionmaker[AsyncSession]


async def resolve_tenant(sessionmaker: Sessionmaker, key: str) -> Tenant:
    async with sessionmaker() as session:
        tenant = await session.scalar(
            select(Tenant)
            .join(ApiKey, ApiKey.tenant_id == Tenant.id)
            .where(ApiKey.key_hash == hash_key(key), ApiKey.revoked_at.is_(None))
        )
    if tenant is None:
        raise SystemExit("That key does not belong to a live tenant.")
    if tenant.monthly_budget_microcents is None:
        raise SystemExit(
            f"Tenant {tenant.name!r} has no budget, so nothing is reserved.\n"
            f"Run: uv run tollgate set-budget {tenant.name} --usd 25"
        )
    return tenant


async def run(args: argparse.Namespace) -> str:
    settings = get_settings()
    engine = create_async_engine(args.database_url or settings.database_url, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    tenant = await resolve_tenant(sessionmaker, args.tenant_key)
    pricebook = PriceBook(sessionmaker, refresh_s=0)
    price = await pricebook.price_for(MODEL, datetime.now(UTC))
    if price is None:
        raise SystemExit(f"No price for {MODEL}; run `uv run tollgate prices`.")

    started = datetime.now(UTC)
    estimates: list[int] = []

    async with httpx.AsyncClient(base_url=args.gateway, timeout=60) as client:
        for index in range(args.requests):
            ceiling = CEILINGS[index % len(CEILINGS)]
            payload = {
                "contents": [{"role": "user", "parts": [{"text": f"Request {index}."}]}],
                "generationConfig": {"maxOutputTokens": ceiling},
            }
            # Serialised here, and sent as raw bytes, so the estimate is computed over
            # exactly the bytes the gateway saw rather than a re-encoding of them.
            body = json.dumps(payload).encode()
            estimates.append(
                estimate_request(body, settings.budget_default_max_output_tokens).cost(price)
            )
            async with client.stream(
                "POST",
                PATH,
                content=body,
                headers={"x-goog-api-key": args.tenant_key, "content-type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise SystemExit(f"Gateway returned {response.status_code}: {response.text}")
                async for _ in response.aiter_bytes():
                    pass

    # The ledger row for a stream is written by a shielded task just after the last
    # event, so it can land a moment after the response is finished.
    rows: list[UsageRecord] = []
    for _ in range(40):
        async with sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(UsageRecord)
                    .where(
                        UsageRecord.tenant_id == tenant.id,
                        UsageRecord.created_at >= started,
                    )
                    .order_by(UsageRecord.created_at)
                )
            )
        if len(rows) >= args.requests:
            break
        await asyncio.sleep(0.1)

    actuals = [row.cost_microcents or 0 for row in rows][: args.requests]
    ratios = [
        estimate / actual
        for estimate, actual in zip(estimates, actuals, strict=False)
        if actual > 0
    ]
    ledger_total = sum(actuals)

    report = [
        f"{len(actuals)} streamed requests, maxOutputTokens cycling {CEILINGS[0]}-{CEILINGS[-1]}.",
        "",
        "reservation against actual cost",
        f"  median overshoot        {statistics.median(ratios):>10.1f}x",
        f"  worst                   {max(ratios):>10.1f}x",
        f"  best                    {min(ratios):>10.1f}x",
        "",
    ]

    # The counter covers the tenant's whole month, so that is what it has to agree
    # with - not just the rows this run added.
    month_total = await month_to_date(sessionmaker, tenant, started)
    counter = await read_counter(args.redis_url, tenant, started)

    report += [
        "reconciliation after settlement",
        f"  this run                {ledger_total:>12,} uc  {format_usd(ledger_total)}",
        f"  ledger, month to date   {month_total:>12,} uc  {format_usd(month_total)}",
    ]
    if counter is None:
        report.append("  redis month counter              n/a  (no Redis reachable)")
    else:
        report += [
            f"  redis month counter     {counter:>12,} uc  {format_usd(counter)}",
            f"  error                   {counter - month_total:>12,} uc",
            "",
            "  The counter is seeded from the ledger and moved only by settlements, so",
            "  anything but zero means an estimate was left standing as a charge.",
        ]

    await engine.dispose()
    return "\n".join(report)


async def month_to_date(sessionmaker: Sessionmaker, tenant: Tenant, at: datetime) -> int:
    start, end = month_bounds(at.strftime("%Y-%m"))
    async with sessionmaker() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost_microcents), 0)).where(
                UsageRecord.tenant_id == tenant.id,
                UsageRecord.created_at >= start,
                UsageRecord.created_at < end,
            )
        )
    return int(total or 0)


async def read_counter(url: str, tenant: Tenant, at: datetime) -> int | None:
    client: Redis = Redis.from_url(url, socket_connect_timeout=2)
    try:
        await client.ping()
        raw = await client.get(BudgetGuard.spend_key(tenant.id, at.strftime("%Y-%m")))
    except (redis.exceptions.RedisError, OSError):
        return None
    finally:
        await client.aclose()
    return int(raw) if raw is not None else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_key")
    parser.add_argument("--requests", type=int, default=21)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    args = parser.parse_args()
    print(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
