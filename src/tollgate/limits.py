"""Per-tenant rate limits and spend budgets.

Two refusals live here, and they are not the same kind of thing. A rate limit protects
the upstream from a runaway tenant and is a technical cap with a sane generic value. A
budget is money: it encodes what a customer agreed to pay, it has no defensible default,
and being wrong about it is a commercial problem rather than an operational one. That
difference decides almost every trade below, starting with what each does when Redis is
unreachable.

RATE LIMITING - a token bucket, held in this process or in Redis.

**Why a token bucket and not a sliding window.** Real traffic is bursty. A client that
wakes up, fires twelve requests and goes quiet is behaving normally, and a window that
refuses the twelfth request punishes it for being efficient. A bucket separates the two
numbers that actually matter: `rpm` is the sustained rate a tenant may hold, `burst` is
how much of it may arrive at once. A window only has the first.

**Why Redis.** A bucket in process memory is correct for one container and wrong for
several: each container refills its own bucket, so N containers let a tenant through at
roughly N times its limit. Nothing in the code is wrong - the state is just in the wrong
place. `MemoryRateLimiter` is kept rather than deleted so the size of that error can be
measured, which is the point of this phase.

**Why one Lua script.** Refilling a bucket is read-modify-write, and two containers that
read "one token left" at the same moment both consume it. Redis runs a script to
completion without interleaving anything else, so the whole refill-and-take is one
indivisible step. The script also reads the clock from Redis rather than accepting one
from the caller, so containers with drifting clocks still share a single timeline.

**Why this fails open.** If Redis is unreachable the gateway allows the request and says
so loudly. A limiter exists to protect the upstream from a runaway tenant; turning its
outage into a total outage trades a small problem for a much larger one. Money is not
what this protects - budgets, below, degrade differently. For the same reason Redis is
deliberately not a readiness check: replacing tasks cannot fix Redis, and a restart loop
would make things worse. Set `rate_limit_fail_open=false` to choose the opposite trade.

BUDGETS - reserve before the call, reconcile after it.

**The problem.** A budget has to be enforced before the upstream is called, because
afterwards the money is already spent. But a streamed response's cost is unknown at that
moment: the tokens have not been generated yet. So the gateway reserves an estimate built
from the request's own ceiling - `maxOutputTokens`, or a configured default - and replaces
it with the real figure when the response ends. The estimate is deliberately generous: one
that is too small lets a tenant overshoot, while one that is too large only makes them
wait for a reconciliation that arrives milliseconds later.

**A reservation is a lease, not a lock.** It carries an expiry, and the next request to
look at the tenant drops the ones that have run out. A gateway killed mid-stream therefore
cannot hold a tenant's budget hostage until someone notices - the worst it can do is
overstate that tenant's committed spend for the rest of one lease.

**Nothing in Redis is a source of truth.** The month's counter is a cache of a sum over the
ledger, reseeded from Postgres whenever it is missing, and reservations are in-flight
bookkeeping that the ledger settles. That invariant is what makes it safe to run Redis with
eviction enabled, and it is why every key here has a TTL.

**Why budgets do not fail open.** Postgres holds the ledger and is already a hard
dependency, so losing Redis costs precision, not enforcement: the gateway reads the sum
directly and carries on. What it loses is sight of other containers' in-flight requests,
so concurrent streams can jointly overshoot by roughly their own estimates. Slower and
slightly blunter beats switched off, which is the opposite of the choice made for the
limiter above, for the reason given at the top.
"""

import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth import TenantContext, require_tenant
from tollgate.config import Settings
from tollgate.db.models import UsageRecord
from tollgate.errors import GatewayError
from tollgate.logs import request_id_var
from tollgate.telemetry import stage
from tollgate.usage import MONTH_FORMAT, Price, PriceBook, format_usd, month_bounds

logger = logging.getLogger("tollgate.limits")

SECONDS_PER_MINUTE = 60
KEY_PREFIX = "tollgate:rl:"
SPEND_KEY_PREFIX = "tollgate:spend:"
RESERVED_KEY_PREFIX = "tollgate:reserved:"
# The provider's own documentation gives four characters to a token as the rule of thumb.
CHARS_PER_TOKEN = 4

# Refill rates and token counts are floats. That is not a money value: nothing here is
# owed to anyone, and a bucket that is 0.3 tokens out is a bucket that waits 0.3 tokens
# longer. Money stays in integer micro-cents; see usage.py.


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float
    remaining: float


class RateLimiter(Protocol):
    """Takes one token for a tenant, or refuses and says how long to wait."""

    name: str

    async def take(self, tenant_id: uuid.UUID, rpm: int, burst: int) -> Decision: ...


class MemoryRateLimiter:
    """A bucket per tenant, in this process.

    One entry per tenant that has called recently, a few dozen bytes each, bounded by
    the size of the tenants table. Entries are not evicted: a tenant that stops calling
    leaves a bucket behind, which is cheaper than the bookkeeping to remove it.
    """

    name = "memory"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[uuid.UUID, tuple[float, float]] = {}

    async def take(self, tenant_id: uuid.UUID, rpm: int, burst: int) -> Decision:
        rate = rpm / SECONDS_PER_MINUTE
        now = self._clock()
        # An unseen tenant starts full, exactly like a key Redis has expired.
        tokens, at = self._buckets.get(tenant_id, (float(burst), now))
        tokens = min(burst, tokens + (now - at) * rate)

        if tokens >= 1:
            self._buckets[tenant_id] = (tokens - 1, now)
            return Decision(allowed=True, retry_after_s=0.0, remaining=tokens - 1)
        self._buckets[tenant_id] = (tokens, now)
        return Decision(allowed=False, retry_after_s=(1 - tokens) / rate, remaining=tokens)


# Refill and take, as one indivisible step. Returns integers, because Lua's number type
# reaches Redis as an integer and anything fractional would be truncated on the way out;
# tokens therefore leave as thousandths.
TAKE_SCRIPT = """
local rate  = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])

-- Redis's clock, not the caller's, so every container shares one timeline.
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(bucket[1])
local at = tonumber(bucket[2])
if tokens == nil or at == nil then
  tokens = burst
  at = now
end

tokens = math.min(burst, tokens + (now - at) * rate)

local allowed = 1
local retry_after_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  allowed = 0
  retry_after_ms = math.ceil((1 - tokens) * 1000 / rate)
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'at', now)
-- A full bucket and a missing key mean the same thing, so the key only has to outlive
-- one refill from empty. Idle tenants evict themselves.
redis.call('PEXPIRE', KEYS[1], math.ceil(burst * 1000 / rate) + 1000)

return {allowed, retry_after_ms, math.floor(tokens * 1000)}
"""


class RedisRateLimiter:
    """One bucket per tenant, shared by every container."""

    name = "redis"

    def __init__(self, client: Redis, *, fail_open: bool = True) -> None:
        self._client = client
        self._fail_open = fail_open
        self._script = client.register_script(TAKE_SCRIPT)

    async def take(self, tenant_id: uuid.UUID, rpm: int, burst: int) -> Decision:
        try:
            result = cast(
                list[int],
                await self._script(
                    keys=[f"{KEY_PREFIX}{tenant_id}"],
                    args=[rpm / SECONDS_PER_MINUTE, burst],
                ),
            )
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "rate limiter unavailable",
                extra={"fields": {"error": type(exc).__name__, "fail_open": self._fail_open}},
            )
            return Decision(allowed=self._fail_open, retry_after_s=1.0, remaining=0.0)

        allowed, retry_after_ms, milli_tokens = result
        return Decision(
            allowed=bool(allowed),
            retry_after_s=retry_after_ms / 1000,
            remaining=milli_tokens / 1000,
        )


def build_redis(settings: Settings) -> Redis | None:
    """One client for everything that needs Redis, or None when none is configured.

    Shared rather than one per user, so the connection pool, the timeouts and the
    health check are configured in a single place - and closed in a single place.
    """
    url = settings.redis_url.get_secret_value()
    if not url:
        return None
    client: Redis = Redis.from_url(
        url,
        socket_connect_timeout=settings.redis_connect_timeout_s,
        # Waiting is worse than guessing: this timeout is what keeps a stalled Redis
        # from adding itself to the latency of every request that passes through.
        socket_timeout=settings.redis_command_timeout_s,
        health_check_interval=30,
    )
    return client


def build_rate_limiter(settings: Settings, redis: Redis | None) -> RateLimiter:
    if settings.limiter_backend == "memory":
        return MemoryRateLimiter()
    if redis is None:
        raise RuntimeError("LIMITER_BACKEND=redis needs REDIS_URL to be set.")
    return RedisRateLimiter(redis, fail_open=settings.rate_limit_fail_open)


def too_many_requests(retry_after_s: float) -> GatewayError:
    """429 with a hint. Retry-After is whole seconds, so a wait is always rounded up:
    telling a client to come back sooner than the bucket allows guarantees a second 429."""
    seconds = max(1, math.ceil(retry_after_s))
    return GatewayError(
        429,
        "rate_limited",
        f"Rate limit exceeded. Retry in {seconds}s.",
        headers={"retry-after": str(seconds)},
    )


async def rate_limited(
    request: Request, tenant: Annotated[TenantContext, Depends(require_tenant)]
) -> TenantContext:
    """Dependency: authenticate, then spend one token. Returns the tenant either way,
    so a route asks for this instead of `require_tenant` and gets both."""
    if tenant.rate_limit_rpm is None:
        return tenant  # unlimited by policy: an internal tenant, or a load test
    if tenant.rate_limit_rpm <= 0:
        raise too_many_requests(SECONDS_PER_MINUTE)  # throttled to a standstill

    limiter: RateLimiter = request.app.state.rate_limiter
    burst = max(1, tenant.rate_limit_burst or tenant.rate_limit_rpm)
    with stage(
        "rate_limit",
        **{"tollgate.limiter.backend": limiter.name, "tollgate.limit.rpm": tenant.rate_limit_rpm},
    ) as span:
        decision = await limiter.take(tenant.tenant_id, tenant.rate_limit_rpm, burst)
        span.set_attribute("tollgate.limit.allowed", decision.allowed)
        span.set_attribute("tollgate.limit.remaining", round(decision.remaining, 3))

    if not decision.allowed:
        # No ledger row: the request never reached the upstream, so it cost nothing and has
        # no place in a spend ledger. Rejections are counted as a metric instead.
        logger.info(
            "rate limited",
            extra={"fields": {"rpm": tenant.rate_limit_rpm, "burst": burst}},
        )
        raise too_many_requests(decision.retry_after_s)
    return tenant


# =========================================================================================
# Budgets
# =========================================================================================


@dataclass(frozen=True)
class Estimate:
    """What a request might cost, before the upstream has generated anything."""

    input_tokens: int
    output_tokens: int
    thoughts_tokens: int

    def cost(self, price: Price) -> int:
        return price.cost(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            thoughts_tokens=self.thoughts_tokens,
        )


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        yield from (text for item in value.values() for text in _strings(item))
    elif isinstance(value, list):
        yield from (text for item in value for text in _strings(item))


def estimate_request(body: bytes, default_max_output_tokens: int) -> Estimate:
    """A deliberately generous guess at a request's size, without a tokenizer.

    Every string anywhere in the body is counted, including JSON the model never reads,
    and the output is assumed to run to the ceiling the request asked for. Both push the
    estimate upward on purpose: too small lets a tenant overshoot its budget, too large
    only holds back a little more of it until the reconciliation a moment later.

    An unparseable body is charged by its raw length, so a malformed request cannot
    reserve nothing at all.
    """
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return Estimate(
            input_tokens=math.ceil(len(body) / CHARS_PER_TOKEN),
            output_tokens=default_max_output_tokens,
            thoughts_tokens=0,
        )

    characters = sum(len(text) for text in _strings(payload))
    config = payload.get("generationConfig") if isinstance(payload, dict) else None
    config = config if isinstance(config, dict) else {}
    thinking = config.get("thinkingConfig")
    budget = thinking.get("thinkingBudget") if isinstance(thinking, dict) else None
    requested = config.get("maxOutputTokens")

    return Estimate(
        input_tokens=math.ceil(characters / CHARS_PER_TOKEN),
        output_tokens=(
            requested if isinstance(requested, int) and requested > 0 else default_max_output_tokens
        ),
        thoughts_tokens=budget if isinstance(budget, int) and budget > 0 else 0,
    )


@dataclass(frozen=True)
class Reservation:
    """A claim on part of a tenant's remaining budget, held until the request ends."""

    tenant_id: uuid.UUID
    month: str
    request_id: str
    estimate_microcents: int


def budget_exhausted(committed: int, budget: int) -> GatewayError:
    """402, not 429. A tenant out of budget is not being asked to slow down - retrying
    will not work until the month turns over, and a Retry-After measured in weeks is
    worse than no hint at all."""
    return GatewayError(
        402,
        "budget_exhausted",
        f"Month-to-date spend and committed requests come to {format_usd(committed)} "
        f"of a {format_usd(budget)} monthly budget.",
    )


# Take a reservation, or refuse. Returns {-1, 0, 0} when the month has no counter yet,
# which tells the caller to seed it from the ledger and ask again.
RESERVE_SCRIPT = """
local spend = redis.call('GET', KEYS[1])
if spend == false then
  return {-1, 0, 0}
end
spend = tonumber(spend)

local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

-- A reservation is a lease. One that has outlived its lease belongs to a request that
-- died, not to a claim on the budget, so it is dropped here rather than held until
-- somebody notices. Every pass over a tenant cleans up after the last one.
local reserved = 0
local held = redis.call('HGETALL', KEYS[2])
for i = 1, #held, 2 do
  local amount, expires = string.match(held[i + 1], '^(%d+):([%d%.]+)$')
  if amount == nil or tonumber(expires) <= now then
    redis.call('HDEL', KEYS[2], held[i])
  else
    reserved = reserved + tonumber(amount)
  end
end

local estimate = tonumber(ARGV[2])
if spend + reserved + estimate > tonumber(ARGV[1]) then
  return {0, spend, reserved}
end

redis.call('HSET', KEYS[2], ARGV[3], ARGV[2] .. ':' .. (now + tonumber(ARGV[4])))
redis.call('PEXPIRE', KEYS[2], math.ceil(tonumber(ARGV[4]) * 1000) + 1000)
return {1, spend, reserved}
"""

# Seed the month's counter from the ledger, but only if it is still missing: another
# container may have seeded and incremented it since this sum was taken, and overwriting
# that would throw away every settlement made in between.
SEED_SCRIPT = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 1
end
return 0
"""

# Release the reservation and record what the request really cost.
SETTLE_SCRIPT = """
redis.call('HDEL', KEYS[2], ARGV[1])
-- Only add to a counter that still exists. If the month's key has been evicted, the next
-- reservation reseeds it from the ledger - which already holds this request's row, because
-- the row is written before this runs. Recreating the key here would instead seed the
-- month with the cost of one request and lose everything before it.
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('INCRBY', KEYS[1], ARGV[2])
end
return 1
"""


class BudgetGuard:
    """Enforces a tenant's monthly spend cap across every container."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        pricebook: PriceBook,
        redis: Redis | None,
        *,
        lease_s: float,
        default_max_output_tokens: int,
        month_ttl_s: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._pricebook = pricebook
        self._redis = redis
        self._lease_s = lease_s
        self._default_max_output_tokens = default_max_output_tokens
        self._month_ttl_s = month_ttl_s
        self._reserve = redis.register_script(RESERVE_SCRIPT) if redis else None
        self._seed = redis.register_script(SEED_SCRIPT) if redis else None
        self._settle = redis.register_script(SETTLE_SCRIPT) if redis else None

    # --- keys ----------------------------------------------------------------------------

    @staticmethod
    def spend_key(tenant_id: uuid.UUID, month: str) -> str:
        return f"{SPEND_KEY_PREFIX}{tenant_id}:{month}"

    @staticmethod
    def reserved_key(tenant_id: uuid.UUID) -> str:
        return f"{RESERVED_KEY_PREFIX}{tenant_id}"

    # --- the ledger, which is the authority -------------------------------------------------

    async def spent_in_month(self, tenant_id: uuid.UUID, month: str) -> int:
        """Month-to-date spend straight from the ledger, in micro-cents.

        Rows with a NULL cost - an upstream timeout, an unpriced model - count as zero
        here. They are a gap to investigate, not a charge to invent.
        """
        start, end = month_bounds(month)
        async with self._sessionmaker() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(UsageRecord.cost_microcents), 0)).where(
                    UsageRecord.tenant_id == tenant_id,
                    UsageRecord.created_at >= start,
                    UsageRecord.created_at < end,
                )
            )
        return int(total or 0)

    # --- reserve and settle ------------------------------------------------------------------

    async def reserve(self, tenant: TenantContext, model: str, body: bytes) -> Reservation | None:
        """Claim the request's likely cost, or refuse with 402.

        Called before anything is sent upstream, so a refusal costs nothing and writes no
        ledger row. Returns None for a tenant with no budget, which then skips settlement.
        """
        budget = tenant.monthly_budget_microcents
        if budget is None:
            return None

        now = datetime.now(UTC)
        month = now.strftime(MONTH_FORMAT)
        price = await self._pricebook.price_for(model, now)
        # No price means no estimate. The month-to-date total is still checked, so a
        # tenant already over its budget is still refused; what cannot be done is guess
        # at what an unpriced model will add.
        estimate = (
            estimate_request(body, self._default_max_output_tokens).cost(price) if price else 0
        )
        request_id = request_id_var.get() or uuid.uuid4().hex

        with stage("budget.reserve", **{"tollgate.budget.estimate_microcents": estimate}) as span:
            allowed, spent, reserved = await self._claim(
                tenant.tenant_id, month, budget, estimate, request_id
            )
            span.set_attribute("tollgate.budget.spent_microcents", spent)
            span.set_attribute("tollgate.budget.reserved_microcents", reserved)
            span.set_attribute("tollgate.budget.allowed", allowed)
        if not allowed:
            logger.info(
                "budget exhausted",
                extra={"fields": {"spent": spent, "reserved": reserved, "budget": budget}},
            )
            raise budget_exhausted(spent + reserved, budget)
        return Reservation(tenant.tenant_id, month, request_id, estimate)

    async def settle(self, reservation: Reservation | None, actual_microcents: int | None) -> None:
        """Release the reservation and replace the estimate with what was really spent.

        Runs after the ledger row is written, which is what makes it safe for the script
        to skip a counter that has gone: the reseed that follows will find the row.
        A settlement that never happens - a crash, a Redis outage - is corrected when the
        lease expires, so this is allowed to fail quietly.
        """
        if reservation is None or self._settle is None:
            return
        try:
            with stage("budget.settle", **{"tollgate.cost_microcents": actual_microcents}):
                await self._settle(
                    keys=[
                        self.spend_key(reservation.tenant_id, reservation.month),
                        self.reserved_key(reservation.tenant_id),
                    ],
                    args=[reservation.request_id, actual_microcents or 0],
                )
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "budget settlement deferred to the lease",
                extra={"fields": {"error": type(exc).__name__}},
            )

    # --- where the numbers come from -----------------------------------------------------------

    async def _claim(
        self, tenant_id: uuid.UUID, month: str, budget: int, estimate: int, request_id: str
    ) -> tuple[bool, int, int]:
        if self._reserve is not None:
            try:
                claimed = await self._claim_in_redis(tenant_id, month, budget, estimate, request_id)
            except (RedisError, OSError, TimeoutError) as exc:
                logger.warning(
                    "budget cache unavailable, reading the ledger",
                    extra={"fields": {"error": type(exc).__name__}},
                )
            else:
                if claimed is not None:
                    return claimed

        # Without Redis the ledger still enforces the budget, it just cannot see what
        # other containers have in flight. Concurrent requests can jointly overshoot by
        # about their own estimates; that is the cost of losing the cache, not of
        # losing enforcement.
        spent = await self.spent_in_month(tenant_id, month)
        return spent + estimate <= budget, spent, 0

    async def _claim_in_redis(
        self, tenant_id: uuid.UUID, month: str, budget: int, estimate: int, request_id: str
    ) -> tuple[bool, int, int] | None:
        assert self._reserve is not None and self._seed is not None
        keys = [self.spend_key(tenant_id, month), self.reserved_key(tenant_id)]
        # Twice at most: once as it stands, and once after seeding a month that has no
        # counter. A second miss means another container evicted it in between, which is
        # rare enough to be worth a ledger read rather than a third round trip.
        for _ in range(2):
            allowed, spent, reserved = cast(
                list[int],
                await self._reserve(keys=keys, args=[budget, estimate, request_id, self._lease_s]),
            )
            if allowed >= 0:
                return bool(allowed), spent, reserved
            await self._seed(
                keys=[keys[0]],
                args=[await self.spent_in_month(tenant_id, month), int(self._month_ttl_s)],
            )
        return None


def build_budget_guard(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    pricebook: PriceBook,
    redis: Redis | None,
) -> BudgetGuard:
    return BudgetGuard(
        sessionmaker,
        pricebook,
        redis,
        lease_s=settings.budget_lease_s,
        default_max_output_tokens=settings.budget_default_max_output_tokens,
        month_ttl_s=settings.budget_month_ttl_s,
    )
