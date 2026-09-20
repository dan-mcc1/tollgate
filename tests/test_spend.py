"""The spend rollup and the endpoint on top of it."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import Keys
from tests.test_limits import auth
from tollgate.db.models import ApiKey, Tenant, UsageRecord
from tollgate.usage import MICROCENTS_PER_USD, month_bounds, rollup

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
SPEND = "/v1/spend"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]

THIS_MONTH = datetime.now(UTC).strftime("%Y-%m")


# --- month arithmetic ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("month", "start", "end"),
    [
        ("2026-09", "2026-09-01", "2026-10-01"),
        ("2026-12", "2026-12-01", "2027-01-01"),  # the year has to roll over too
        ("2026-01", "2026-01-01", "2026-02-01"),
    ],
)
def test_month_bounds_are_half_open(month: str, start: str, end: str) -> None:
    """Half-open, so there is no gap and no overlap between one month and the next.
    With an inclusive end, a row written on the last microsecond of a month would be
    counted twice or by neither side."""
    lower, upper = month_bounds(month)

    assert lower.isoformat().startswith(start)
    assert upper.isoformat().startswith(end)


# --- helpers ---------------------------------------------------------------------------------


async def acme(sessionmaker: Sessionmaker) -> tuple[Tenant, ApiKey]:
    async with sessionmaker() as session:
        tenant = (await session.scalars(select(Tenant).where(Tenant.name == "acme"))).one()
        key = (await session.scalars(select(ApiKey).where(ApiKey.tenant_id == tenant.id))).first()
        assert key is not None
        return tenant, key


async def append(
    sessionmaker: Sessionmaker,
    tenant: Tenant,
    key: ApiKey,
    rows: list[tuple[str, int | None, datetime]],
) -> None:
    """Write ledger rows directly, so a test can place them in any month it likes."""
    async with sessionmaker() as session:
        for model, cost, created in rows:
            session.add(
                UsageRecord(
                    tenant_id=tenant.id,
                    api_key_id=key.id,
                    created_at=created,
                    model=model,
                    method="generateContent",
                    input_tokens=10,
                    output_tokens=20,
                    thoughts_tokens=0,
                    status_code=200,
                    cost_microcents=cost,
                )
            )
        await session.commit()


async def set_budget(sessionmaker: Sessionmaker, microcents: int | None) -> None:
    async with sessionmaker() as session:
        await session.execute(
            update(Tenant).where(Tenant.name == "acme").values(monthly_budget_microcents=microcents)
        )
        await session.commit()


# --- the rollup ------------------------------------------------------------------------------


async def test_the_rollup_groups_by_model_and_ignores_other_months(
    sessionmaker: Sessionmaker, keys: Keys
) -> None:
    tenant, key = await acme(sessionmaker)
    now = datetime.now(UTC)
    last_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
    await append(
        sessionmaker,
        tenant,
        key,
        [
            (MODEL, 1_000, now),
            (MODEL, 2_500, now),
            ("gemini-3.7-pro", 10_000, now),
            (MODEL, 999_999, last_month),  # a different month: must not be counted
        ],
    )

    totals = await rollup(sessionmaker, tenant.id, THIS_MONTH)

    assert totals.requests == 3
    assert totals.cost_microcents == 13_500
    # Ordered by spend, so the model costing the most is the first thing read.
    assert [entry.model for entry in totals.by_model] == ["gemini-3.7-pro", MODEL]
    assert totals.by_model[1].requests == 2
    assert totals.by_model[1].cost_microcents == 3_500
    assert totals.by_model[1].input_tokens == 20


async def test_unpriced_rows_still_count_as_requests(
    sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """A timeout or an unpriced model contributes zero rather than vanishing, so the
    request count stays honest even where the cost is unknown."""
    tenant, key = await acme(sessionmaker)
    await append(sessionmaker, tenant, key, [(MODEL, None, datetime.now(UTC))])

    totals = await rollup(sessionmaker, tenant.id, THIS_MONTH)

    assert totals.requests == 1
    assert totals.cost_microcents == 0


async def test_a_month_with_nothing_in_it_is_empty_not_missing(
    sessionmaker: Sessionmaker, keys: Keys
) -> None:
    tenant, _ = await acme(sessionmaker)

    totals = await rollup(sessionmaker, tenant.id, "2020-01")

    assert (totals.requests, totals.cost_microcents, totals.by_model) == (0, 0, [])


# --- the endpoint ------------------------------------------------------------------------------


async def test_a_tenant_reads_its_own_spend(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await set_budget(sessionmaker, MICROCENTS_PER_USD)
    for _ in range(3):
        await gateway.post(URL, json=BODY, headers=auth(keys.live))

    response = await gateway.get(SPEND, headers=auth(keys.live))

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant"] == "acme"
    assert payload["month"] == THIS_MONTH
    assert payload["requests"] == 3
    # Both forms of every amount: the integer to compute with, the string to read.
    assert payload["spend"]["microcents"] > 0
    assert payload["spend"]["usd"].startswith("$")
    assert payload["budget"]["microcents"] == MICROCENTS_PER_USD
    assert payload["remaining"]["microcents"] == MICROCENTS_PER_USD - payload["spend"]["microcents"]
    assert payload["by_model"][0]["model"] == MODEL


async def test_spend_needs_a_key(gateway: httpx.AsyncClient, keys: Keys) -> None:
    response = await gateway.get(SPEND)

    assert response.status_code == 401


async def test_a_tenant_with_no_budget_gets_nulls_not_zeroes(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """A budget of nothing and no budget at all are different states. A tenant reading a
    remaining balance of zero would be told the opposite of the truth."""
    await set_budget(sessionmaker, None)

    payload = (await gateway.get(SPEND, headers=auth(keys.live))).json()

    assert payload["budget"] is None
    assert payload["remaining"] is None


async def test_remaining_never_goes_negative(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """Spend can pass the budget - a reservation is an estimate, and the last request
    through reconciles upward. What a tenant has left is still nothing, not a debt."""
    tenant, key = await acme(sessionmaker)
    await set_budget(sessionmaker, 1_000)
    await append(sessionmaker, tenant, key, [(MODEL, 5_000, datetime.now(UTC))])

    payload = (await gateway.get(SPEND, headers=auth(keys.live))).json()

    assert payload["spend"]["microcents"] == 5_000
    assert payload["remaining"]["microcents"] == 0


async def test_an_earlier_month_can_be_asked_for(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    payload = (await gateway.get(f"{SPEND}?month=2020-01", headers=auth(keys.live))).json()

    assert payload["month"] == "2020-01"
    assert payload["requests"] == 0
    assert payload["spend"]["microcents"] == 0


@pytest.mark.parametrize("month", ["2026-13", "not-a-month", "2026", "2026-1", "2026-00"])
async def test_a_malformed_month_is_refused(
    gateway: httpx.AsyncClient, keys: Keys, month: str
) -> None:
    response = await gateway.get(f"{SPEND}?month={month}", headers=auth(keys.live))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_month"


async def test_a_tenant_cannot_see_another_tenants_spend(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    """There is no tenant parameter to tamper with: the only tenant this endpoint will
    ever report on is the one the key resolved to."""
    async with sessionmaker() as session:
        other = Tenant(name="other")
        session.add(other)
        await session.flush()
        other_key = ApiKey(tenant_id=other.id, name="k", key_prefix="tg_other", key_hash="0" * 64)
        session.add(other_key)
        await session.flush()
        session.add(
            UsageRecord(
                tenant_id=other.id,
                api_key_id=other_key.id,
                created_at=datetime.now(UTC),
                model=MODEL,
                method="generateContent",
                status_code=200,
                cost_microcents=777_777,
            )
        )
        await session.commit()

    payload = (await gateway.get(SPEND, headers=auth(keys.live))).json()

    assert payload["tenant"] == "acme"
    assert payload["spend"]["microcents"] == 0  # not the other tenant's 777,777
