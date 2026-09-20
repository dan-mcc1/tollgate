"""Budgets: the estimate, the 402, and the reconciliation.

Most tests here run the guard without Redis, where the ledger is read directly - that
path has to be correct on its own, because it is what a Redis outage falls back to. The
tests that need Redis are the ones about state shared between containers: reservations
held across requests, and a month counter that has to end up agreeing with the ledger to
the micro-cent.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mock_upstream import main as mock
from tests.conftest import Keys, make_settings, usage_rows
from tests.test_limits import auth  # same header helper
from tests.test_streaming import queue_script
from tollgate.auth import TenantContext
from tollgate.config import Settings
from tollgate.db.models import ApiKey, Tenant, UsageRecord
from tollgate.errors import GatewayError
from tollgate.limits import BudgetGuard, Reservation, estimate_request
from tollgate.usage import MICROCENTS_PER_USD, Price, PriceBook

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]

ONE_DOLLAR = MICROCENTS_PER_USD
FLASH_INPUT = 30_000_000
FLASH_OUTPUT = 250_000_000


@pytest.fixture
def upstream_transport() -> None:
    """No in-process transport: reach the mock over loopback instead.

    httpx's ASGI transport collects a whole response before handing it back, so with it
    in place a stream is not a stream - every event arrives at once and a client can
    never hang up part way through. The reservation tests below turn on exactly that,
    so the whole module talks to a real server.
    """
    return None


@pytest.fixture
def settings(live_upstream: str) -> Settings:
    return make_settings().model_copy(update={"upstream_base_url": live_upstream})


def replace_budget(tenant: TenantContext, microcents: int | None) -> TenantContext:
    """The same tenant with a different cap. TenantContext is frozen on purpose - it is
    what authentication resolved, not a place to accumulate state."""
    return TenantContext(
        tenant_id=tenant.tenant_id,
        tenant_name=tenant.tenant_name,
        api_key_id=tenant.api_key_id,
        monthly_budget_microcents=microcents,
    )


async def set_budget(sessionmaker: Sessionmaker, microcents: int | None) -> None:
    async with sessionmaker() as session:
        await session.execute(update(Tenant).values(monthly_budget_microcents=microcents))
        await session.commit()


async def ledger_total(sessionmaker: Sessionmaker) -> int:
    async with sessionmaker() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost_microcents), 0))
        )
    return int(total or 0)


# --- the estimate ---------------------------------------------------------------------------


def test_the_output_ceiling_comes_from_the_request() -> None:
    body = b'{"contents":[{"parts":[{"text":"hi"}]}],"generationConfig":{"maxOutputTokens":100}}'

    estimate = estimate_request(body, default_max_output_tokens=8192)

    assert estimate.output_tokens == 100


def test_a_request_with_no_ceiling_gets_the_configured_default() -> None:
    """There is nothing else to go on, and assuming a short answer would let a tenant
    walk past its budget one long response at a time."""
    estimate = estimate_request(b'{"contents":[]}', default_max_output_tokens=8192)

    assert estimate.output_tokens == 8192


def test_thinking_budget_is_counted_when_asked_for() -> None:
    body = b'{"generationConfig":{"thinkingConfig":{"thinkingBudget":500}}}'

    assert estimate_request(body, 8192).thoughts_tokens == 500
    assert estimate_request(b"{}", 8192).thoughts_tokens == 0


def test_the_input_estimate_errs_high() -> None:
    """Every string in the body is counted, punctuation and field values included. The
    reservation is corrected within milliseconds; an under-estimate is not."""
    text = "x" * 400
    body = f'{{"contents":[{{"parts":[{{"text":"{text}"}}]}}]}}'.encode()

    estimate = estimate_request(body, 8192)

    assert estimate.input_tokens >= len(text) // 4


def test_an_unparseable_body_still_reserves_something() -> None:
    """A malformed request must not be a way to reserve nothing at all."""
    estimate = estimate_request(b"not json at all, just bytes", 8192)

    assert estimate.input_tokens > 0
    assert estimate.output_tokens == 8192


def flash_price() -> Price:
    return Price(
        id=uuid.uuid4(),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        input_microcents_per_mtok=FLASH_INPUT,
        output_microcents_per_mtok=FLASH_OUTPUT,
        thoughts_microcents_per_mtok=FLASH_OUTPUT,
    )


def test_the_estimate_is_priced_with_the_ledgers_own_arithmetic() -> None:
    """The reservation and the final charge must not be two different cost models, or
    they could never reconcile."""
    price = flash_price()
    estimate = estimate_request(b'{"generationConfig":{"maxOutputTokens":1000}}', 8192)

    assert estimate.cost(price) == price.cost(
        input_tokens=estimate.input_tokens,
        output_tokens=estimate.output_tokens,
        thoughts_tokens=estimate.thoughts_tokens,
    )


# --- refusing, before the upstream is called ---------------------------------------------------


async def test_a_tenant_over_budget_is_refused_before_the_upstream(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """402, and nothing sent, nothing recorded. The request cost nothing, so it has no
    place in a spend ledger."""
    await set_budget(sessionmaker, 0)

    response = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["source"] == "gateway"
    assert error["code"] == "budget_exhausted"
    assert "$0.000000" in error["message"]
    assert mock.stats.calls["generateContent"] == 0
    assert await usage_rows(sessionmaker) == []


async def test_a_tenant_within_budget_is_served(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await set_budget(sessionmaker, ONE_DOLLAR)

    response = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert response.status_code == 200
    [row] = await usage_rows(sessionmaker)
    assert row.cost_microcents is not None


async def test_a_tenant_with_no_budget_is_never_refused(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """NULL is the default. A budget is a commercial arrangement, so there is no
    defensible number to invent for a tenant nobody has set one for."""
    await set_budget(sessionmaker, None)

    responses = [await gateway.post(URL, json=BODY, headers=auth(keys.live)) for _ in range(5)]

    assert {response.status_code for response in responses} == {200}


async def test_spending_accumulates_until_the_budget_stops_it(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """The ledger is what the guard reads, so earlier requests are what refuse later
    ones - not a counter this test primed."""
    await set_budget(sessionmaker, None)
    for _ in range(3):
        await gateway.post(URL, json=BODY, headers=auth(keys.live))
    spent = await ledger_total(sessionmaker)

    # A budget just under what has already been spent must refuse the next request.
    await set_budget(sessionmaker, spent - 1)
    refused = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert spent > 0
    assert refused.status_code == 402
    assert len(await usage_rows(sessionmaker)) == 3  # no row for the refusal


async def test_a_streamed_request_is_refused_before_the_stream_opens(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await set_budget(sessionmaker, 0)

    response = await live_gateway.post(STREAM_URL, json=BODY, headers=auth(keys.live))

    assert response.status_code == 402
    assert mock.stats.calls["streamGenerateContent"] == 0


async def spent_tenant(sessionmaker: Sessionmaker, cost_microcents: int) -> TenantContext:
    """A tenant with one ledger row already against its name."""
    async with sessionmaker() as session:
        tenant = Tenant(name=f"spent-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        key = ApiKey(tenant_id=tenant.id, name="k", key_prefix="tg_x", key_hash=uuid.uuid4().hex)
        session.add(key)
        await session.flush()
        session.add(
            UsageRecord(
                tenant_id=tenant.id,
                api_key_id=key.id,
                model=MODEL,
                method="generateContent",
                input_tokens=10,
                output_tokens=10,
                status_code=200,
                cost_microcents=cost_microcents,
            )
        )
        await session.commit()
        return TenantContext(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            api_key_id=key.id,
        )


def guard_for(sessionmaker: Sessionmaker, redis: Redis | None = None) -> BudgetGuard:
    return BudgetGuard(
        sessionmaker,
        PriceBook(sessionmaker, refresh_s=0),
        redis,
        lease_s=300.0,
        default_max_output_tokens=8192,
        month_ttl_s=1000.0,
    )


async def test_an_unpriced_model_does_not_bypass_an_exhausted_budget(
    sessionmaker: Sessionmaker,
) -> None:
    """Nothing can be estimated for a model with no price, so the estimate is zero. The
    month-to-date total is still checked, so a tenant already over its cap stays refused
    rather than slipping through on an unknown model."""
    tenant = await spent_tenant(sessionmaker, cost_microcents=5_000)
    over_budget = replace_budget(tenant, 1_000)

    with pytest.raises(GatewayError) as refused:
        await guard_for(sessionmaker).reserve(over_budget, "model-with-no-price", b"{}")

    assert refused.value.status_code == 402
    assert refused.value.code == "budget_exhausted"


async def test_an_unpriced_model_is_served_when_the_budget_has_room(
    sessionmaker: Sessionmaker,
) -> None:
    tenant = await spent_tenant(sessionmaker, cost_microcents=5_000)
    within_budget = replace_budget(tenant, ONE_DOLLAR)

    assert (
        await guard_for(sessionmaker).reserve(within_budget, "model-with-no-price", b"{}")
        is not None
    )


# --- reconciliation -------------------------------------------------------------------------


@pytest.fixture
async def budget_redis(maybe_redis: Redis | None) -> AsyncIterator[Redis]:
    """Overrides conftest's default of None, so the gateway fixtures get a real guard."""
    if maybe_redis is None:
        pytest.skip("no Redis")
    yield maybe_redis


async def spend_counter(redis: Redis, sessionmaker: Sessionmaker) -> int:
    async with sessionmaker() as session:
        tenant_id = await session.scalar(select(Tenant.id))
    keys = await redis.keys(f"tollgate:spend:{tenant_id}:*")
    values = [int(await redis.get(key) or 0) for key in keys]
    return sum(values)


async def reserved_count(redis: Redis, sessionmaker: Sessionmaker) -> int:
    async with sessionmaker() as session:
        tenant_id = await session.scalar(select(Tenant.id))
    return int(await redis.hlen(f"tollgate:reserved:{tenant_id}"))


async def test_a_reservation_reconciles_to_the_real_cost(
    gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    budget_redis: Redis,
) -> None:
    """The estimate is generous, so the counter would be far too high if it were never
    settled. After the request it agrees with the ledger exactly - not to a cent, to a
    micro-cent."""
    await set_budget(sessionmaker, ONE_DOLLAR)

    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert await spend_counter(budget_redis, sessionmaker) == await ledger_total(sessionmaker)
    assert await reserved_count(budget_redis, sessionmaker) == 0


async def test_a_streamed_reservation_reconciles_to_the_real_cost(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    budget_redis: Redis,
) -> None:
    """The case the whole design exists for: the cost was unknown when the budget was
    checked, because the tokens had not been generated yet."""
    await set_budget(sessionmaker, ONE_DOLLAR)

    async with live_gateway.stream(
        "POST", STREAM_URL, json=BODY, headers=auth(keys.live)
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    ledger = await ledger_total(sessionmaker)
    assert ledger > 0
    assert await spend_counter(budget_redis, sessionmaker) == ledger
    assert await reserved_count(budget_redis, sessionmaker) == 0


async def test_many_requests_reconcile_exactly(
    gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    budget_redis: Redis,
) -> None:
    """Reservation error does not accumulate: the counter is the sum of actuals, never
    a running total of estimates."""
    await set_budget(sessionmaker, 100 * ONE_DOLLAR)

    for index in range(10):
        body = {"contents": [{"parts": [{"text": f"request number {index}"}]}]}
        await gateway.post(URL, json=body, headers=auth(keys.live))

    assert len(await usage_rows(sessionmaker)) == 10
    assert await spend_counter(budget_redis, sessionmaker) == await ledger_total(sessionmaker)


async def test_an_abandoned_stream_still_settles(
    live_gateway: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    keys: Keys,
    budget_redis: Redis,
    live_upstream: str,
) -> None:
    """A client that hangs up mid-stream must not leave its reservation held.

    Those tokens were generated and will be billed, so the row is written and the
    estimate is replaced by it - and, just as importantly, the tenant gets the rest of
    its reservation back immediately rather than waiting out the lease.
    """
    await set_budget(sessionmaker, ONE_DOLLAR)
    # The second step never arrives: the client walks away long before it would.
    await queue_script(
        live_upstream,
        [
            {"text": "one", "usage": {"candidatesTokenCount": 5}},
            {"delayMs": 5000, "text": "never read"},
        ],
    )

    async with live_gateway.stream("POST", STREAM_URL, json=BODY, headers=auth(keys.live)) as r:
        async for _ in r.aiter_bytes():
            break  # the caller walks away mid-stream

    rows = []
    for _ in range(50):  # the ledger write is shielded, so it lands just after the hang-up
        rows = await usage_rows(sessionmaker)
        if rows:
            break
        await asyncio.sleep(0.05)

    [row] = rows
    assert row.client_disconnected is True
    assert await reserved_count(budget_redis, sessionmaker) == 0
    assert await spend_counter(budget_redis, sessionmaker) == await ledger_total(sessionmaker)


async def test_a_held_reservation_blocks_a_request_that_would_exceed_the_budget(
    sessionmaker: Sessionmaker, budget_redis: Redis
) -> None:
    """The point of reserving at all. One request in flight, its cost still unknown,
    must already count against the budget the next one is measured against."""
    tenant = await spent_tenant(sessionmaker, cost_microcents=0)
    guard = guard_for(sessionmaker, budget_redis)
    body = b'{"contents":[{"parts":[{"text":"hi"}]}],"generationConfig":{"maxOutputTokens":1000}}'
    # Room for roughly one request's estimate, and no more.
    one_request = 1000 * FLASH_OUTPUT // 1_000_000
    context = replace_budget(tenant, one_request + 100)

    first = await guard.reserve(context, MODEL, body)
    with pytest.raises(GatewayError) as refused:
        await guard.reserve(context, MODEL, body)

    assert isinstance(first, Reservation)
    assert refused.value.status_code == 402

    # Settling the first one at its real (much smaller) cost frees the room again.
    await guard.settle(first, 10)
    assert await guard.reserve(context, MODEL, body) is not None


async def test_settling_does_not_resurrect_an_evicted_counter(
    sessionmaker: Sessionmaker, budget_redis: Redis
) -> None:
    """With eviction enabled the month's key can vanish. Recreating it here would seed
    the month with one request's cost and lose everything before it; the next
    reservation reseeds it from the ledger instead."""
    tenant_id = uuid.uuid4()
    guard = guard_for(sessionmaker, budget_redis)
    reservation = Reservation(tenant_id, "2026-09", "request-1", 500)

    await guard.settle(reservation, 1234)

    assert await budget_redis.exists(guard.spend_key(tenant_id, "2026-09")) == 0
