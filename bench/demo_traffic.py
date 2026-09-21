"""Traffic with enough variety in it that the dashboard shows what it is for.

Not a benchmark and not a load test: nothing here is measured, and the numbers it
produces mean nothing. It exists because a dashboard with one tenant and one outcome
demonstrates nothing, and the panels that matter - outcomes by colour, spend per tenant,
overhead against upstream - only say anything once there is a mix to look at.

Four tenants, set up to behave differently on purpose:

    acme      ordinary traffic, unary and streamed
    globex    streaming heavy, long responses, thinking tokens
    initech   a rate limit low enough that it is hit constantly
    hooli     a budget that runs out part way through, and refusals after that

    docker compose up -d --wait
    OTEL_ENABLED=True uv run uvicorn tollgate.main:app --port 8000   # another terminal
    uv run python bench/demo_traffic.py --seconds 180

Everything goes to the mock upstream, so it costs nothing.
"""

import argparse
import asyncio
import random
from collections import Counter
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tollgate.auth import generate_key
from tollgate.config import get_settings
from tollgate.db.models import ApiKey, Tenant
from tollgate.usage import MICROCENTS_PER_USD

MODEL = "gemini-3.7-flash"
UNARY = f"/v1beta/models/{MODEL}:generateContent"
STREAM = f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"


@dataclass(frozen=True)
class Profile:
    name: str
    rpm: int | None
    burst: int | None
    workers: int
    stream_share: float
    prompts: list[str] = field(default_factory=list)
    # Pin the output ceiling when the reservation needs to be predictable. Left
    # unset, a request gets a random ceiling or none at all, and with none the
    # estimate assumes the configured maximum - which on a small budget refuses the
    # very first request rather than the twentieth.
    max_output_tokens: int | None = None
    budget_microcents: int | None = None
    # A budget given as a rate, so the run length can change without moving the moment
    # it runs out. A fixed total tuned for five minutes is spent in the first ninety
    # seconds of a thirty-minute run, and the panel goes back to a flat band of
    # refusals - which is the thing this profile exists to avoid showing.
    # 58,000 micro-cents a second is measured: one worker, a 128-token ceiling.
    spend_rate_microcents_per_second: float | None = None
    exhaust_at: float | None = None  # fraction of the run at which the budget runs out


PROFILES = [
    Profile(
        name="acme",
        rpm=6000,
        burst=100,
        budget_microcents=25 * MICROCENTS_PER_USD,
        workers=4,
        stream_share=0.3,
        prompts=["Summarise this.", "What is the capital of France?", "[[mock:tokens=120]]"],
    ),
    Profile(
        name="globex",
        rpm=6000,
        burst=100,
        budget_microcents=25 * MICROCENTS_PER_USD,
        workers=3,
        stream_share=0.8,
        prompts=["[[mock:tokens=400]]", "[[mock:thinking=200]]", "[[mock:tokens=250]]"],
    ),
    Profile(
        # Throttled, but only somewhat: this fills the rate_limited band on the
        # outcomes panel, and it is the most common real refusal. The limit is set at
        # roughly half what these workers offer, because a client refused nine times
        # out of ten is not a service under load, it is a misconfiguration - and a
        # dashboard where most of the traffic is refused shows nothing except itself.
        name="initech",
        rpm=60,
        burst=15,
        budget_microcents=25 * MICROCENTS_PER_USD,
        workers=1,
        stream_share=0.2,
        prompts=["Hello.", "[[mock:tokens=60]]"],
    ),
    Profile(
        # Enough budget for about half a run at this ceiling, so the panel shows the
        # transition rather than a flat band: served for a while, then refused for the
        # rest of the month. The ceiling is pinned so each reservation is a known size;
        # left unset, the estimate assumes the configured maximum and refuses the very
        # first request instead of the hundredth.
        name="hooli",
        rpm=6000,
        burst=100,
        workers=1,
        stream_share=0.2,
        prompts=["[[mock:tokens=200]]"],
        max_output_tokens=128,
        spend_rate_microcents_per_second=58_000,
        exhaust_at=0.7,
    ),
]

# A small share of everything, so the error bands are visible without dominating.
FAULTS = ["[[mock:error=503]]", "[[mock:error=429]]", "[[mock:slow=700]]"]


def budget_for(profile: Profile, seconds: float) -> int | None:
    if profile.spend_rate_microcents_per_second is None or profile.exhaust_at is None:
        return profile.budget_microcents
    return int(profile.spend_rate_microcents_per_second * seconds * profile.exhaust_at)


async def provision(database_url: str, seconds: float) -> dict[str, str]:
    """Make sure each tenant exists with the limits its profile needs, and mint a key."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    keys: dict[str, str] = {}
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        for profile in PROFILES:
            tenant = await session.scalar(select(Tenant).where(Tenant.name == profile.name))
            if tenant is None:
                tenant = Tenant(name=profile.name)
                session.add(tenant)
                await session.flush()
            tenant.rate_limit_rpm = profile.rpm
            tenant.rate_limit_burst = profile.burst
            tenant.monthly_budget_microcents = budget_for(profile, seconds)

            key = generate_key()
            session.add(
                ApiKey(
                    tenant_id=tenant.id,
                    name="demo-traffic",
                    key_prefix=key.prefix,
                    key_hash=key.hash,
                )
            )
            keys[profile.name] = key.plaintext
        await session.commit()
    await engine.dispose()
    return keys


def body(profile: Profile, rng: random.Random) -> dict[str, object]:
    text = rng.choice(profile.prompts)
    if rng.random() < 0.06:
        text = f"{text} {rng.choice(FAULTS)}"
    payload: dict[str, object] = {"contents": [{"role": "user", "parts": [{"text": text}]}]}
    if profile.max_output_tokens is not None:
        payload["generationConfig"] = {"maxOutputTokens": profile.max_output_tokens}
    elif rng.random() < 0.4:
        payload["generationConfig"] = {"maxOutputTokens": rng.choice([128, 512, 2048])}
    return payload


async def worker(
    client: httpx.AsyncClient,
    profile: Profile,
    key: str,
    deadline: float,
    outcomes: Counter[str],
    rng: random.Random,
) -> None:
    headers = {"x-goog-api-key": key, "content-type": "application/json"}
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        streamed = rng.random() < profile.stream_share
        try:
            if streamed:
                async with client.stream(
                    "POST", STREAM, json=body(profile, rng), headers=headers
                ) as response:
                    async for _ in response.aiter_bytes():
                        pass
                    status = response.status_code
            else:
                status = (
                    await client.post(UNARY, json=body(profile, rng), headers=headers)
                ).status_code
        except httpx.HTTPError as exc:
            outcomes[f"{profile.name}:{type(exc).__name__}"] += 1
        else:
            outcomes[f"{profile.name}:{status}"] += 1
        # Paced, not hammered: the point is a few minutes of plausible traffic, and a
        # tight loop would just show one spike and a flat line either side of it.
        await asyncio.sleep(rng.uniform(0.1, 0.6))


async def run(args: argparse.Namespace) -> Counter[str]:
    settings = get_settings()
    keys = await provision(args.database_url or settings.database_url, args.seconds)
    outcomes: Counter[str] = Counter()
    deadline = asyncio.get_running_loop().time() + args.seconds

    async with httpx.AsyncClient(base_url=args.gateway, timeout=60) as client:
        await asyncio.gather(
            *(
                worker(client, profile, keys[profile.name], deadline, outcomes, random.Random(seed))
                for profile in PROFILES
                for seed in range(profile.workers)
            )
        )
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    print(f"Driving {sum(p.workers for p in PROFILES)} workers for {args.seconds:g}s...")
    outcomes = asyncio.run(run(args))

    print(f"\n{'tenant:status':<28}{'count':>8}")
    for label, count in sorted(outcomes.items()):
        print(f"{label:<28}{count:>8}")
    print(f"{'total':<28}{sum(outcomes.values()):>8}")


if __name__ == "__main__":
    main()
