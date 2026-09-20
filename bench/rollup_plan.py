"""Phase 4: what the rollup index is worth, measured from the query plans.

Builds a throwaway `tollgate_bench` database, fills the ledger with a realistic spread of
rows across tenants and months, then runs the two queries that actually read this table -
the budget check before every request, and the spend rollup - with the index dropped and
again with it in place. Prints both plans in full, so the change is visible as a plan
node and not only as a number.

    docker compose up -d --wait
    uv run python bench/rollup_plan.py --rows 500000 --tenants 25

Nothing here touches the development or test databases.
"""

import argparse
import asyncio
import statistics
from pathlib import Path

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+asyncpg://postgres:password@localhost:5432/tollgate_bench"
INDEX = "ix_usage_records_tenant_id_created_at"

# The budget check, once per request, on the hot path.
BUDGET_SUM = """
SELECT coalesce(sum(cost_microcents), 0)
FROM usage_records
WHERE tenant_id = :tenant
  AND created_at >= :start AND created_at < :end
"""

# The spend endpoint's rollup.
ROLLUP = """
SELECT model, count(*), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0),
       coalesce(sum(cost_microcents), 0)
FROM usage_records
WHERE tenant_id = :tenant
  AND created_at >= :start AND created_at < :end
GROUP BY model
"""

SEED_TENANTS = """
INSERT INTO tenants (id, name, is_active, created_at)
SELECT gen_random_uuid(), 'bench-' || g.i, true, now()
FROM generate_series(1, :tenants) AS g(i)
"""

SEED_KEYS = """
INSERT INTO api_keys (id, tenant_id, name, key_prefix, key_hash, created_at)
SELECT gen_random_uuid(), t.id, 'bench', 'tg_bench', md5(t.id::text), now()
FROM tenants t
"""

# One statement for the whole ledger: a round trip per row would dominate the runtime and
# tell us nothing about the query being measured.
SEED_USAGE = """
WITH pairs AS (
    SELECT array_agg(t.id ORDER BY t.name) AS tenant_ids,
           array_agg(k.id ORDER BY t.name) AS key_ids
    FROM tenants t JOIN api_keys k ON k.tenant_id = t.id
)
INSERT INTO usage_records (
    id, tenant_id, api_key_id, created_at, model, method,
    input_tokens, output_tokens, thoughts_tokens,
    upstream_latency_ms, upstream_attempts, status_code, streamed,
    client_disconnected, cost_microcents
)
SELECT
    gen_random_uuid(),
    p.tenant_ids[1 + (g.i % array_length(p.tenant_ids, 1))],
    p.key_ids[1 + (g.i % array_length(p.key_ids, 1))],
    now() - (random() * interval '120 days'),
    (ARRAY['gemini-3.7-flash', 'gemini-3.7-pro', 'gemini-embedding-001'])[1 + (g.i % 3)],
    'generateContent',
    (random() * 2000)::int, (random() * 2000)::int, 0,
    (random() * 400)::int, 1, 200, g.i % 4 = 0, false,
    (random() * 50000)::bigint
FROM generate_series(1, :rows) AS g(i), pairs p
"""


async def recreate(url: str) -> None:
    target = make_url(url)
    admin = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    await admin.dispose()


def migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


async def explain(conn: AsyncConnection, sql: str, params: dict[str, object]) -> list[str]:
    result = await conn.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)
    return [row[0] for row in result]


async def time_query(
    conn: AsyncConnection, sql: str, params: dict[str, object], runs: int
) -> float:
    """Median execution time in milliseconds, as the planner itself reports it."""
    samples: list[float] = []
    for _ in range(runs):
        plan = await explain(conn, sql, params)
        line = next(row for row in plan if row.startswith("Execution Time:"))
        samples.append(float(line.split()[2]))
    return statistics.median(samples)


async def drop_index(conn: AsyncConnection) -> None:
    await conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))


async def create_index(conn: AsyncConnection) -> None:
    await drop_index(conn)
    await conn.execute(text(f"CREATE INDEX {INDEX} ON usage_records (tenant_id, created_at)"))


async def create_covering_index(conn: AsyncConnection) -> None:
    """The same index, carrying the columns both queries read.

    Worth measuring because the plain index still visits nearly one heap block per row:
    a tenant's rows are scattered through the table, since the ledger is written in
    arrival order and not grouped by tenant. If the index carries the values, Postgres
    can answer from it alone - provided the visibility map says those pages are frozen,
    which on an append-only table means waiting for a vacuum.
    """
    await drop_index(conn)
    await conn.execute(
        text(
            f"CREATE INDEX {INDEX} ON usage_records (tenant_id, created_at) "
            "INCLUDE (model, input_tokens, output_tokens, thoughts_tokens, cost_microcents)"
        )
    )


async def run(args: argparse.Namespace) -> str:
    engine = create_async_engine(args.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(SEED_TENANTS), {"tenants": args.tenants})
        await conn.execute(text(SEED_KEYS))
        await conn.execute(text(SEED_USAGE), {"rows": args.rows})

    report: list[str] = []
    async with engine.connect() as conn:
        await conn.execute(text("COMMIT"))
        tenant = await conn.scalar(text("SELECT id FROM tenants ORDER BY name LIMIT 1"))
        bounds = await conn.execute(
            text(
                "SELECT date_trunc('month', now()), date_trunc('month', now()) + interval '1 month'"
            )
        )
        start, end = bounds.one()
        params: dict[str, object] = {"tenant": tenant, "start": start, "end": end}

        rows_in_month = await conn.scalar(
            text(
                "SELECT count(*) FROM usage_records WHERE tenant_id = :tenant "
                "AND created_at >= :start AND created_at < :end"
            ),
            params,
        )

        report += [
            f"{args.rows:,} ledger rows across {args.tenants} tenants and about four months.",
            f"The measured tenant has {rows_in_month:,} rows in the current month.",
            "",
        ]

        stages = (
            ("without an index", drop_index),
            ("with (tenant_id, created_at)", create_index),
            ("with the covering index", create_covering_index),
        )
        for stage, prepare in stages:
            await prepare(conn)
            await conn.execute(text("COMMIT"))
            # VACUUM as well as ANALYZE: stale statistics would have the planner choosing
            # on guesses rather than facts, and a stale visibility map would deny the
            # covering index the index-only scan that is the whole reason to try it.
            await conn.execute(text("VACUUM ANALYZE usage_records"))
            await conn.execute(text("COMMIT"))

            size = await conn.scalar(
                text("SELECT pg_size_pretty(pg_relation_size(to_regclass(:name)))"),
                {"name": INDEX},
            )
            report.append(f"--- {stage}: index size {size or 'none'}")
            for label, sql in (("budget check", BUDGET_SUM), ("spend rollup", ROLLUP)):
                median = await time_query(conn, sql, params, args.runs)
                report.append(f"=== {label}, {stage}: {median:.1f} ms (median of {args.runs})")
                report += await explain(conn, sql, params)
                report.append("")

    await engine.dispose()
    return "\n".join(report)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=500_000)
    parser.add_argument("--tenants", type=int, default=25)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--database-url", default=DEFAULT_URL)
    parser.add_argument("--out", default="bench/results/rollup_plan.txt")
    args = parser.parse_args()

    print(f"Building {make_url(args.database_url).database} with {args.rows:,} rows...")
    asyncio.run(recreate(args.database_url))
    # Alembic opens an event loop of its own, so it cannot run inside one.
    migrate(args.database_url)

    report = asyncio.run(run(args))
    print(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        print(f"Written to {out}")


if __name__ == "__main__":
    main()
