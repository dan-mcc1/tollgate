"""What the exact cache is worth, and what it costs to ask.

Three numbers come out of this, and one caveat that matters more than any of them.

  * **Hit rate**, by tier and including the requests the cache declined to look at.
  * **Spend avoided per thousand requests**, in dollars.
  * **Latency, hit against miss** - the reason anyone wants a cache in the first place.

**The caveat.** A cache's hit rate is a property of the traffic, not of the cache. Run
this against a thousand identical prompts and it reports 99.9%; run it against a thousand
distinct ones and it reports zero. Neither figure says anything about the gateway. What
is modelled here instead is a Zipf distribution over a pool of distinct prompts, which is
the shape real request traffic tends to have - a small head asked constantly, a long tail
asked once - and the parameters are printed alongside the result so the number can be
read as what it is: this cache, on this traffic.

**How the saving is measured.** Not by a second run with the cache switched off. Every
ledger row carries what the request cost (`cost_microcents`, zero on a hit) and what it
would have cost (`cost_avoided_microcents`, set only on a hit), so the two sum to the bill
the same traffic would have run up with no cache at all. That is an exact comparison
against the identical traffic rather than an approximate one against a second sample, and
it needs no second run to make it.

    docker compose up -d --wait
    uv run uvicorn tollgate.main:app --port 8000        # in another terminal
    uv run python bench/cache_savings.py tg_your_tenant_key

The gateway must have CACHE_ENABLED=True, which is the default.
"""

import argparse
import asyncio
import pathlib
import random
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tollgate.auth import hash_key
from tollgate.cache.keys import BYPASS, EXACT_HIT, MISS, SEMANTIC_HIT
from tollgate.config import get_settings
from tollgate.db.models import ApiKey, CacheEntry, Tenant, UsageRecord
from tollgate.usage import format_usd, month_bounds

MODEL = "gemini-3.7-flash"
PATH = f"/v1beta/models/{MODEL}:generateContent"
CACHE_HEADER = "x-tollgate-cache"

Sessionmaker = async_sessionmaker[AsyncSession]

# The prompts the pool is built from. Kept dull and varied in length on purpose: what is
# being measured is repetition, and a pool of near-identical sentences would instead be
# measuring how close to each other they are, which is the semantic tier's question.
TOPICS = [
    "what is a reverse proxy",
    "explain connection pooling to a new engineer",
    "how does a token bucket differ from a sliding window",
    "why store a hash of an API key rather than the key",
    "summarise the tradeoffs of server-sent events against websockets",
    "what does backpressure mean in a streaming pipeline",
    "when should a service fail open rather than closed",
    "describe an append-only ledger and why it is not updated in place",
    "what is the difference between a task role and an execution role",
    "how do histogram bucket boundaries affect a percentile",
]


def prompt_pool(distinct: int, seed: int) -> list[str]:
    """`distinct` reproducible prompts, each genuinely different from the others."""
    rng = random.Random(seed)
    return [
        f"{TOPICS[i % len(TOPICS)]}? (variant {i}, {rng.randint(1000, 9999)})"
        for i in range(distinct)
    ]


def variant(prompt: str, rng: random.Random) -> str:
    """The same question, written slightly differently.

    Only surface forms: trailing punctuation, capitalisation, doubled spaces. The exact
    tier deliberately refuses to normalise any of these away - "Hello" and "hello " are
    different inputs to a model, and deciding otherwise on the model's behalf is not the
    gateway's call - so every one of them is an exact miss. Whether they are a semantic
    hit is exactly what the threshold decides, which makes them the traffic that
    separates a tier that earns its embedding call from one that does not.
    """
    choice = rng.randrange(4)
    if choice == 0:
        return prompt.rstrip("?.! ")
    if choice == 1:
        return prompt[0].upper() + prompt[1:] if prompt else prompt
    if choice == 2:
        return prompt.replace(" ", "  ", 1)
    return prompt + " "


def zipf_choices(pool: list[str], count: int, skew: float, seed: int) -> list[str]:
    """`count` draws from `pool`, the head asked far more often than the tail.

    Real traffic to a gateway is not uniform - a handful of prompts dominate - and a
    uniform draw would understate a cache by exactly as much as an all-identical draw
    would overstate it.
    """
    rng = random.Random(seed)
    weights = [1 / (rank**skew) for rank in range(1, len(pool) + 1)]
    return rng.choices(pool, weights=weights, k=count)


@dataclass
class Sample:
    status: str
    elapsed_ms: float


@dataclass
class Run:
    samples: list[Sample] = field(default_factory=list)

    def by_status(self, status: str) -> list[float]:
        return [s.elapsed_ms for s in self.samples if s.status == status]

    @property
    def outcomes(self) -> Counter[str]:
        return Counter(s.status for s in self.samples)


def percentile(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
    return ordered[index]


async def resolve_tenant(sessionmaker: Sessionmaker, key: str) -> Tenant:
    async with sessionmaker() as session:
        tenant = await session.scalar(
            select(Tenant)
            .join(ApiKey, ApiKey.tenant_id == Tenant.id)
            .where(ApiKey.key_hash == hash_key(key), ApiKey.revoked_at.is_(None))
        )
    if tenant is None:
        raise SystemExit("That key does not belong to a live tenant.")
    return tenant


async def clear_cache(sessionmaker: Sessionmaker, tenant_id: uuid.UUID) -> None:
    """Start cold, so a second run of this bench does not report a 100% hit rate."""
    async with sessionmaker() as session:
        await session.execute(delete(CacheEntry).where(CacheEntry.tenant_id == tenant_id))
        await session.commit()


async def ledger_totals(
    sessionmaker: Sessionmaker, tenant_id: uuid.UUID, since: datetime
) -> tuple[int, int, int, int]:
    """(requests, paid, avoided, spent embedding) in micro-cents, for this run's rows."""
    start, _ = month_bounds(since.strftime("%Y-%m"))
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(UsageRecord.cost_microcents), 0),
                    func.coalesce(func.sum(UsageRecord.cost_avoided_microcents), 0),
                    func.coalesce(func.sum(UsageRecord.embedding_cost_microcents), 0),
                ).where(
                    UsageRecord.tenant_id == tenant_id,
                    UsageRecord.created_at >= max(start, since),
                )
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


def send(client: httpx.Client, key: str, prompt: str, temperature: float) -> Sample:
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    started = time.perf_counter()
    response = client.post(PATH, json=body, headers={"x-goog-api-key": key})
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return Sample(response.headers.get(CACHE_HEADER, "disabled"), elapsed_ms)


@dataclass(frozen=True)
class Shape:
    """One traffic shape, and what the cache did with it."""

    distinct: int
    repeats: int
    total: int
    hits: int
    semantic_hits: int
    bypassed: int
    hit_p50: float
    miss_p50: float
    paid: int
    avoided: int
    embedded: int

    @property
    def hit_rate(self) -> float:
        return (self.hits + self.semantic_hits) / self.total

    @property
    def net(self) -> int:
        """What the cache saved after what it spent looking.

        The embedding spend is over every request the semantic tier examined, found or
        not, which is the only way to ask whether the tier pays for itself. With the
        tier off it is zero and this is just the saving.
        """
        return self.avoided - self.embedded

    @property
    def would_have_cost(self) -> int:
        return self.paid + self.avoided

    @property
    def per_thousand(self) -> int:
        return round(self.avoided / self.total * 1000)


async def measure(
    args: argparse.Namespace, sessionmaker: Sessionmaker, tenant: Tenant, distinct: int
) -> Shape:
    """One run against one pool size, from a cold cache."""
    await clear_cache(sessionmaker, tenant.id)
    pool = prompt_pool(distinct, args.seed)
    prompts = zipf_choices(pool, args.requests, args.skew, args.seed)
    if args.variants:
        # A share of the traffic asks a question already in the pool, in slightly
        # different words. Every one is an exact miss by construction.
        rng = random.Random(args.seed + 1)
        prompts = [
            variant(prompt, rng) if rng.random() < args.variants else prompt for prompt in prompts
        ]
    started_at = datetime.now(UTC)

    run = Run()
    with httpx.Client(base_url=args.gateway, timeout=60) as client:
        for prompt in prompts:
            run.samples.append(send(client, args.tenant_key, prompt, args.temperature))

    # The ledger row is written in the request's finally block, which has already
    # returned by the time the response is in hand, so nothing needs waiting for.
    _, paid, avoided, embedded = await ledger_totals(sessionmaker, tenant.id, started_at)
    outcomes = run.outcomes
    hits = run.by_status(EXACT_HIT) + run.by_status(SEMANTIC_HIT)
    misses = run.by_status(MISS)
    return Shape(
        distinct=distinct,
        repeats=len(prompts) - len(set(prompts)),
        total=sum(outcomes.values()),
        hits=outcomes[EXACT_HIT],
        semantic_hits=outcomes[SEMANTIC_HIT],
        bypassed=outcomes[BYPASS],
        hit_p50=percentile(hits, 50) if hits else 0.0,
        miss_p50=percentile(misses, 50) if misses else 0.0,
        paid=paid,
        avoided=avoided,
        embedded=embedded,
    )


async def collect(args: argparse.Namespace) -> list[Shape]:
    """Every run, in order. Separated from main() so the file write stays synchronous."""
    settings = get_settings()
    engine = create_async_engine(args.database_url or settings.database_url, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        tenant = await resolve_tenant(sessionmaker, args.tenant_key)
        shapes = []
        for distinct in [int(value) for value in args.pools.split(",")]:
            print(f"measuring a pool of {distinct} distinct prompts...")
            shapes.append(await measure(args, sessionmaker, tenant, distinct))
        await clear_cache(sessionmaker, tenant.id)
    finally:
        await engine.dispose()
    return shapes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_key")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument(
        "--pools",
        default="2000,1000,400,120,40",
        help="prompt pool sizes to sweep; a smaller pool means traffic that repeats more",
    )
    parser.add_argument(
        "--skew",
        type=float,
        default=1.1,
        help="Zipf exponent; 0 is uniform, higher concentrates traffic on a smaller head",
    )
    parser.add_argument(
        "--variants",
        type=float,
        default=0.0,
        help="share of requests reworded in surface form; exact misses by construction",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--out", default="bench/results/cache_savings.txt")
    args = parser.parse_args()

    text = report(args, asyncio.run(collect(args)))
    print("\n" + text)
    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\nWritten to {out}")


def report(args: argparse.Namespace, shapes: list[Shape]) -> str:
    lines = [
        f"{args.requests} requests per row at temperature {args.temperature}, drawn with a Zipf",
        f"exponent of {args.skew} from a pool of N distinct prompts, seed {args.seed}, cold cache.",
        "",
        "N is the only thing that varies between these rows. A small pool is traffic that",
        "asks the same handful of questions all day; a large one is traffic that rarely",
        f"repeats itself. It moves the hit rate from {min(s.hit_rate for s in shapes):.0%} "
        f"to {max(s.hit_rate for s in shapes):.0%} without a line of the",
        "gateway changing, which is the point: a cache's hit rate is a property of the",
        "traffic, and what belongs to the gateway is the two latency columns.",
        "",
        f"{'pool':>6} {'repeats':>8} {'exact':>7} {'semantic':>9} {'hit rate':>9} "
        f"{'hit p50':>9} {'miss p50':>9} {'paid':>11} {'avoided':>11} {'saved':>7}",
    ]
    for shape in shapes:
        lines.append(
            f"{shape.distinct:>6} {shape.repeats / shape.total:>7.0%} "
            f"{shape.hits:>7} {shape.semantic_hits:>9} {shape.hit_rate:>9.1%} "
            f"{shape.hit_p50:>8.1f}ms {shape.miss_p50:>8.1f}ms "
            f"{format_usd(shape.paid):>11} {format_usd(shape.avoided):>11} "
            f"{shape.avoided / shape.would_have_cost if shape.would_have_cost else 0:>6.0%}"
        )

    fastest = min(shapes, key=lambda s: s.hit_p50 or 1e9)
    lines += [
        "",
        f"A hit costs {fastest.hit_p50:.1f} ms against {fastest.miss_p50:.1f} ms for the call "
        f"it replaces, which is",
        f"{fastest.miss_p50 / fastest.hit_p50:.0f}x, and is the one number here that does not "
        "depend on the traffic.",
        "",
        "'repeats' is the share of requests that asked something already asked, and so the",
        "ceiling the hit rate is working against. 'saved' compares what was paid with what",
        "the identical traffic would have cost with no cache - taken from the ledger, where",
        "every row carries both what it cost and what it would have cost, rather than from",
        "a second run against a second sample.",
    ]
    if any(shape.bypassed for shape in shapes):
        lines.append(f"{sum(s.bypassed for s in shapes)} requests were bypassed by policy.")

    if any(shape.embedded for shape in shapes):
        worst_miss = max(shape.miss_p50 for shape in shapes)
        lines += [
            "",
            "The semantic tier was on, so every request the exact tier missed also paid",
            "for an embedding - found or not. That shows up twice. In latency: the miss",
            f"column above is {worst_miss:.0f} ms at worst, and it carries an embedding round",
            "trip that a miss without this tier does not, so the tier makes the requests it",
            "fails to help measurably slower. And in money, where netting the two is the",
            "only way to ask whether it earns its keep on this traffic:",
            "",
            f"{'pool':>6} {'semantic':>9} {'avoided':>12} {'spent looking':>14} {'net':>12}",
        ]
        for shape in shapes:
            lines.append(
                f"{shape.distinct:>6} {shape.semantic_hits:>9} "
                f"{format_usd(shape.avoided):>12} {format_usd(shape.embedded):>14} "
                f"{format_usd(shape.net):>12}"
            )
        losing = [shape for shape in shapes if shape.net < 0]
        if losing:
            lines += [
                "",
                f"Net negative on {len(losing)} of {len(shapes)} shapes: the tier spent more on",
                "embeddings than the hits it found were worth. That is not a bug in the tier,",
                "it is the tier being asked about traffic that does not reword itself - and it",
                "is the measurement that says to leave it off for that traffic.",
            ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
