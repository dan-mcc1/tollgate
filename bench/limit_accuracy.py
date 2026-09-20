"""Phase 4: how wrong a rate limit gets when it is held in the wrong place.

A token bucket kept in a container's own memory is correct for one container. Run three
and each one refills a bucket of its own, so a tenant entitled to R requests a minute
gets 3R and nothing in the code looks broken. This measures that error and then measures
it again with the buckets in Redis.

Containers are stood up here as separate limiter instances rather than separate
processes. That is the whole of the difference: the gateway's only involvement is one
`take()` per request, so a limiter that is wrong here is wrong there by the same amount.

    docker compose up -d --wait
    uv run python bench/limit_accuracy.py

Redis is optional; without it only the in-process rows are measured.
"""

import argparse
import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass

import redis.exceptions
from redis.asyncio import Redis

from tollgate.limits import MemoryRateLimiter, RateLimiter, RedisRateLimiter

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


@dataclass(frozen=True)
class Result:
    backend: str
    containers: int
    expected: float
    allowed: int
    attempted: int

    @property
    def error_percent(self) -> float:
        return (self.allowed - self.expected) / self.expected * 100


async def drive(
    limiters: list[RateLimiter], rpm: int, burst: int, seconds: float, demand: int, tick: float
) -> tuple[int, int]:
    """Offer `demand` times the permitted rate for `seconds`, spread evenly.

    Evenly, because bursting the whole load at once would only ever show the bucket
    depth. The question here is what a tenant gets over a window, which is the number a
    limit is actually written in.

    Pacing is by a cumulative target rather than a sleep between each request. Windows
    resolves a timer to about 15 ms, so a per-request sleep of 20 ms actually takes 30,
    and the window closes having offered a third of the intended load - which reads as
    a limiter doing its job rather than a bench that never got going.
    """
    tenant = uuid.uuid4()
    rate = rpm / 60 * demand
    started = time.monotonic()
    issued = allowed = 0

    while (elapsed := time.monotonic() - started) < seconds:
        due = round(rate * elapsed)
        while issued < due:
            decision = await limiters[issued % len(limiters)].take(tenant, rpm, burst)
            allowed += decision.allowed
            issued += 1
        await asyncio.sleep(tick)
    return allowed, issued


def expected_allowed(rpm: int, burst: int, seconds: float) -> float:
    """A full bucket, plus whatever refilled while the window ran."""
    return burst + rpm / 60 * seconds


async def measure(build: list[RateLimiter], backend: str, args: argparse.Namespace) -> Result:
    expected = expected_allowed(args.rpm, args.burst, args.seconds)
    allowed, attempted = await drive(
        build, args.rpm, args.burst, args.seconds, args.demand, args.tick
    )
    return Result(backend, len(build), expected, allowed, attempted)


async def connect(url: str) -> Redis | None:
    client: Redis = Redis.from_url(url, socket_connect_timeout=2)
    try:
        await client.ping()
    except (redis.exceptions.RedisError, OSError) as exc:
        print(f"No Redis at {url} ({type(exc).__name__}); measuring in-process only.\n")
        await client.aclose()
        return None
    return client


async def run(args: argparse.Namespace) -> list[Result]:
    results: list[Result] = []
    for containers in args.containers:
        results.append(
            await measure([MemoryRateLimiter() for _ in range(containers)], "in-process", args)
        )

    redis_client = await connect(args.redis_url)
    if redis_client is not None:
        for containers in args.containers:
            await redis_client.flushdb()
            results.append(
                await measure(
                    [RedisRateLimiter(redis_client) for _ in range(containers)], "redis", args
                )
            )
        await redis_client.aclose()
    return results


def report(results: list[Result], args: argparse.Namespace) -> str:
    lines = [
        f"{args.rpm} requests/minute, burst {args.burst}, over a {args.seconds:g}s window,",
        f"offered at {args.demand}x the permitted rate.",
        "",
        f"{'backend':<12}{'containers':>11}{'offered':>9}{'entitled':>10}"
        f"{'allowed':>9}{'error':>9}",
    ]
    for result in results:
        lines.append(
            f"{result.backend:<12}{result.containers:>11}{result.attempted:>9}"
            f"{result.expected:>10.0f}{result.allowed:>9}{result.error_percent:>8.0f}%"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpm", type=int, default=600)
    parser.add_argument("--burst", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=3.0)
    # High enough that every container is saturated even when there are several of
    # them: with N containers each one only sees demand/N times the permitted rate.
    parser.add_argument("--demand", type=int, default=12, help="offered load, as a multiple")
    parser.add_argument("--tick", type=float, default=0.02, help="pacing interval, seconds")
    parser.add_argument("--containers", type=int, nargs="+", default=[1, 2, 3, 5])
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    runs = [asyncio.run(run(args)) for _ in range(args.repeats)]
    # Median across repeats, so one noisy window cannot set the headline number.
    merged = [
        Result(
            backend=first.backend,
            containers=first.containers,
            expected=first.expected,
            allowed=round(statistics.median(run[index].allowed for run in runs)),
            attempted=round(statistics.median(run[index].attempted for run in runs)),
        )
        for index, first in enumerate(runs[0])
    ]
    print(report(merged, args))


if __name__ == "__main__":
    main()
