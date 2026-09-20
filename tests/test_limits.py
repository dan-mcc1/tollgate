"""Rate limiting: the bucket itself, both backends, and the gateway's 429.

The bucket tests run against `MemoryRateLimiter` and `RedisRateLimiter` alike, because
the two are supposed to be indistinguishable from one tenant's point of view. The only
tests that single one out are the ones about what happens across containers, which is
the entire reason the Redis one exists.

Time is driven by a fake clock in memory and by real (short) sleeps in Redis, whose
script reads Redis's own clock on purpose and so cannot be fooled from here.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator

import httpx
import pytest
import redis.exceptions
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mock_upstream import main as mock
from tests.conftest import TEST_REDIS_URL, Keys, usage_rows
from tollgate.db.models import Tenant
from tollgate.limits import (
    MemoryRateLimiter,
    RateLimiter,
    RedisRateLimiter,
)

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]


def auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


class FakeClock:
    """A clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=["memory", "redis"])
def limiter(
    request: pytest.FixtureRequest, clock: FakeClock, maybe_redis: Redis | None
) -> RateLimiter:
    """The same bucket contract, implemented twice."""
    if request.param == "memory":
        return MemoryRateLimiter(clock=clock)
    if maybe_redis is None:
        pytest.skip(f"no Redis at {TEST_REDIS_URL}")
    return RedisRateLimiter(maybe_redis)


@pytest.fixture
def advance(limiter: RateLimiter, clock: FakeClock) -> Callable[[float], Awaitable[None]]:
    """Move time forward for whichever backend is under test.

    The in-process bucket takes its clock from a test. The Redis one reads Redis's own
    clock on purpose - that is what stops containers with drifting clocks disagreeing -
    so it cannot be fooled from here and the test really waits. Rates in these tests
    are chosen so the real wait is a fraction of a second.
    """

    async def move(seconds: float) -> None:
        if limiter.name == "memory":
            clock.advance(seconds)
        else:
            await asyncio.sleep(seconds)

    return move


# --- the bucket ------------------------------------------------------------------------


async def test_a_burst_is_allowed_then_refused(limiter: RateLimiter) -> None:
    """The whole reason for a bucket rather than a window: arriving all at once is
    normal behaviour, and it is the sustained rate that is capped."""
    tenant = uuid.uuid4()

    allowed = [(await limiter.take(tenant, rpm=60, burst=5)).allowed for _ in range(6)]

    assert allowed == [True, True, True, True, True, False]


async def test_the_bucket_refills_and_the_tenant_recovers(
    limiter: RateLimiter, advance: Callable[[float], Awaitable[None]]
) -> None:
    tenant = uuid.uuid4()
    for _ in range(5):
        await limiter.take(tenant, rpm=600, burst=5)
    refused = await limiter.take(tenant, rpm=600, burst=5)

    # 600/min is ten tokens a second, so 0.11s buys exactly one request back.
    await advance(0.11)
    recovered = await limiter.take(tenant, rpm=600, burst=5)

    assert not refused.allowed
    assert recovered.allowed


async def test_the_retry_hint_is_how_long_a_token_actually_takes(
    limiter: RateLimiter,
) -> None:
    """A hint that is too short guarantees a second 429, and one that is too long
    wastes the tenant's time."""
    tenant = uuid.uuid4()
    for _ in range(2):
        await limiter.take(tenant, rpm=120, burst=2)

    refused = await limiter.take(tenant, rpm=120, burst=2)

    assert not refused.allowed
    assert 0.4 <= refused.retry_after_s <= 0.6  # 120/min is a token every 0.5s


async def test_the_bucket_does_not_fill_past_its_burst(
    limiter: RateLimiter, advance: Callable[[float], Awaitable[None]]
) -> None:
    """Idling does not buy credit. 6000/min is a hundred tokens a second, so a third
    of a second would fill a bucket thirty deep if the burst were not a ceiling."""
    tenant = uuid.uuid4()
    for _ in range(3):
        await limiter.take(tenant, rpm=6000, burst=3)
    await advance(0.3)

    allowed = [(await limiter.take(tenant, rpm=6000, burst=3)).allowed for _ in range(4)]

    assert allowed == [True, True, True, False]


async def test_tenants_do_not_share_a_bucket(limiter: RateLimiter) -> None:
    one, two = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await limiter.take(one, rpm=60, burst=3)

    assert not (await limiter.take(one, rpm=60, burst=3)).allowed
    assert (await limiter.take(two, rpm=60, burst=3)).allowed


# --- what the two backends do differently -------------------------------------------------


async def test_memory_buckets_multiply_the_limit_by_the_container_count() -> None:
    """The bug this phase exists to fix, pinned as a test so it cannot be forgotten.

    Three containers, one tenant, a burst of 5. The tenant is entitled to 5 requests
    and gets 15, because each process refills a bucket of its own.
    """
    tenant = uuid.uuid4()
    containers = [MemoryRateLimiter(clock=FakeClock()) for _ in range(3)]

    allowed = 0
    for container in containers:
        for _ in range(5):
            allowed += (await container.take(tenant, rpm=60, burst=5)).allowed

    assert allowed == 15  # entitled to 5


async def test_redis_buckets_hold_across_containers(redis_client: Redis) -> None:
    """The same three containers, sharing one bucket in Redis, allow exactly 5."""
    tenant = uuid.uuid4()
    containers = [RedisRateLimiter(redis_client) for _ in range(3)]

    allowed = 0
    for container in containers:
        for _ in range(5):
            allowed += (await container.take(tenant, rpm=60, burst=5)).allowed

    assert allowed == 5


async def test_concurrent_takes_are_not_a_race(redis_client: Redis) -> None:
    """Twenty requests at once against a burst of 5. A read-then-write limiter lets
    more than 5 through here; the Lua script cannot, because Redis runs it whole."""
    tenant = uuid.uuid4()
    limiter = RedisRateLimiter(redis_client)

    decisions = await asyncio.gather(*(limiter.take(tenant, rpm=6, burst=5) for _ in range(20)))

    assert sum(decision.allowed for decision in decisions) == 5


async def test_an_idle_bucket_expires_itself(redis_client: Redis) -> None:
    """A full bucket and a missing key mean the same thing, so idle tenants evict
    themselves instead of accumulating in Redis forever."""
    tenant = uuid.uuid4()
    await RedisRateLimiter(redis_client).take(tenant, rpm=60, burst=5)

    ttl_ms = await redis_client.pttl(f"tollgate:rl:{tenant}")

    assert 0 < ttl_ms <= 6_000  # 5 tokens at one a second, plus a second of slack


# --- when Redis is not there ---------------------------------------------------------------


class BrokenRedis:
    """Stands in for a Redis that is down, without needing one to be."""

    def register_script(self, script: str) -> "BrokenRedis":
        return self

    async def __call__(self, keys: list[str], args: list[float]) -> list[int]:
        raise redis.exceptions.ConnectionError("no route to host")

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(("fail_open", "allowed"), [(True, True), (False, False)])
async def test_an_unreachable_redis_takes_the_configured_side(
    fail_open: bool, allowed: bool
) -> None:
    limiter = RedisRateLimiter(BrokenRedis(), fail_open=fail_open)  # type: ignore[arg-type]

    decision = await limiter.take(uuid.uuid4(), rpm=60, burst=5)

    assert decision.allowed is allowed


# --- through the gateway ---------------------------------------------------------------


@pytest.fixture
def rate_limiter(clock: FakeClock) -> Iterator[MemoryRateLimiter]:
    """Overrides conftest's limiter so these tests can control time."""
    yield MemoryRateLimiter(clock=clock)


async def set_limits(sessionmaker: Sessionmaker, rpm: int | None, burst: int | None) -> None:
    async with sessionmaker() as session:
        await session.execute(update(Tenant).values(rate_limit_rpm=rpm, rate_limit_burst=burst))
        await session.commit()


async def test_a_tenant_over_its_limit_gets_a_429_with_a_hint_and_recovers(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys, clock: FakeClock
) -> None:
    await set_limits(sessionmaker, rpm=60, burst=2)

    ok = [await gateway.post(URL, json=BODY, headers=auth(keys.live)) for _ in range(2)]
    refused = await gateway.post(URL, json=BODY, headers=auth(keys.live))
    clock.advance(1.0)
    recovered = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert [response.status_code for response in ok] == [200, 200]
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert refused.json()["error"] == {
        "source": "gateway",
        "code": "rate_limited",
        "message": "Rate limit exceeded. Retry in 1s.",
    }
    assert recovered.status_code == 200


async def test_a_refused_request_never_reaches_the_upstream_or_the_ledger(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """It cost nothing, so it has no place in a spend ledger and no business
    consuming the provider's quota."""
    await set_limits(sessionmaker, rpm=60, burst=1)

    await gateway.post(URL, json=BODY, headers=auth(keys.live))
    refused = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert refused.status_code == 429
    assert mock.stats.calls["generateContent"] == 1
    assert len(await usage_rows(sessionmaker)) == 1


async def test_a_tenant_with_no_limit_is_never_refused(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """NULL rpm is how an internal tenant or a load generator is exempted."""
    await set_limits(sessionmaker, rpm=None, burst=None)

    responses = [await gateway.post(URL, json=BODY, headers=auth(keys.live)) for _ in range(10)]

    assert {response.status_code for response in responses} == {200}


async def test_a_tenant_throttled_to_zero_is_refused_outright(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await set_limits(sessionmaker, rpm=0, burst=0)

    response = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert response.status_code == 429
    assert mock.stats.calls["generateContent"] == 0


async def test_a_missing_burst_is_one_minutes_worth(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await set_limits(sessionmaker, rpm=3, burst=None)

    allowed = [
        (await gateway.post(URL, json=BODY, headers=auth(keys.live))).status_code for _ in range(4)
    ]

    assert allowed == [200, 200, 200, 429]


async def test_an_unauthenticated_request_is_not_charged_to_anyone_s_bucket(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """Authentication runs first, so an attacker with no key cannot drain a tenant's
    bucket - or fill Redis with buckets for tenants that do not exist."""
    await set_limits(sessionmaker, rpm=60, burst=1)

    bad = await gateway.post(URL, json=BODY, headers=auth("tg_not_a_real_key"))
    good = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert bad.status_code == 401
    assert good.status_code == 200
