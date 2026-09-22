"""The cost model: integer money, versioned prices, and a cost on every ledger row."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import BigInteger, Float, Numeric
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import Keys, usage_rows
from tollgate.db.models import Base, ModelPrice, UsageRecord
from tollgate.usage import (
    MICROCENTS_PER_USD,
    Price,
    PriceBook,
    format_usd,
)

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]

# The rates the migration seeds for gemini-3.7-flash: $0.30 and $2.50 per million.
FLASH_INPUT = 30_000_000
FLASH_OUTPUT = 250_000_000


def auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


def price(**rates: int) -> Price:
    return Price(
        id=uuid.uuid4(),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        input_microcents_per_mtok=rates.get("input", FLASH_INPUT),
        output_microcents_per_mtok=rates.get("output", FLASH_OUTPUT),
        thoughts_microcents_per_mtok=rates.get("thoughts", FLASH_OUTPUT),
    )


# --- money is an integer, everywhere ---------------------------------------------------


# Columns that are genuinely a measurement rather than an amount owed to anybody, and
# may therefore be floats. Every entry is a deliberate act: the blanket rule below is
# what catches `spend: Mapped[float]` added in a hurry, so the way past it is to write
# the column down here, next to the reason, where a reviewer has to look at it.
NOT_MONEY = {
    # Cosine similarity, 0 to 1. Nothing is owed in it, nothing is summed in it, and it
    # is written once from what pgvector computed rather than accumulated.
    "usage_records.cache_similarity",
    # A classifier's probability, 0 to 1. Same reasoning: it is what a model reported about
    # one request, written once, and nobody is ever billed in it.
    "usage_records.input_score",
}

# Anything named like an amount. These may never be floats, exception list or not.
MONEY_WORDS = ("cost", "spend", "price", "budget", "microcents", "usd", "amount")


def test_no_column_anywhere_is_a_float() -> None:
    """Money is integer micro-cents. A float column would make cost errors small, silent
    and impossible to reconstruct months later, so the build fails instead of drifting.

    The check is over every column of every table rather than the money ones alone:
    the failure mode is someone adding `spend: Mapped[float]` and nobody noticing.
    """
    offenders = [
        name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, (Float, Numeric))
        and (name := f"{table.name}.{column.name}") not in NOT_MONEY
    ]
    assert offenders == []


def test_nothing_that_sounds_like_money_is_a_float() -> None:
    """The rule the one above is a proxy for, stated directly.

    The exception list exists so that a measurement does not have to be contorted into
    an integer, and this is what stops it being used to sneak an amount through: a
    column named for money is refused whether or not somebody listed it.
    """
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, (Float, Numeric))
        and any(word in column.name.lower() for word in MONEY_WORDS)
    ]
    assert offenders == []


def test_every_listed_exception_still_exists() -> None:
    """An exception that outlives its column is a hole nobody meant to leave open."""
    columns = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    assert columns >= NOT_MONEY, f"listed but gone: {NOT_MONEY - columns}"


def test_money_columns_are_big_integers() -> None:
    """Micro-cents overflow a 32-bit integer at about $21, so they need 64 bits."""
    money = [
        (table.name, column)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if "microcents" in column.name
    ]
    assert money, "this test keys on the '*_microcents*' naming convention and found none"
    for table_name, column in money:
        assert isinstance(column.type, BigInteger), f"{table_name}.{column.name}: {column.type}"


def test_cost_is_exact_integer_arithmetic() -> None:
    million = price().cost(input_tokens=1_000_000, output_tokens=1_000_000, thoughts_tokens=0)

    assert million == FLASH_INPUT + FLASH_OUTPUT  # $0.30 + $2.50, to the micro-cent
    assert type(million) is int
    assert format_usd(million) == "$2.800000"


def test_thinking_tokens_are_charged_at_their_own_rate() -> None:
    """Gemini bills them at the output rate, but the rate is a column, not an assumption."""
    cost = price(thoughts=1_000_000).cost(
        input_tokens=0, output_tokens=0, thoughts_tokens=1_000_000
    )

    assert cost == 1_000_000


def test_a_single_request_does_not_round_away() -> None:
    """The reason the unit is micro-cents. In cents this request costs zero, and a
    month of them costs zero too."""
    cost = price().cost(input_tokens=12, output_tokens=40, thoughts_tokens=0)

    assert cost == 10_360  # 0.0001036 USD
    assert format_usd(cost) == "$0.000104"


def test_rounding_never_favours_the_gateway() -> None:
    """Each component floors, so the tenant is under-charged by under a micro-cent
    per component and never over-charged."""
    one_token = price().cost(input_tokens=1, output_tokens=1, thoughts_tokens=1)
    exact = (FLASH_INPUT + 2 * FLASH_OUTPUT) / 1_000_000

    assert one_token <= exact


# --- the price book --------------------------------------------------------------------


async def test_the_price_in_force_is_the_newest_one_not_in_the_future(
    sessionmaker: Sessionmaker,
) -> None:
    """A price change is a new row, and a request is charged the row that was in force
    when it happened - not the newest row in the table."""
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            ModelPrice(
                model=MODEL,
                effective_from=now + timedelta(days=30),
                input_microcents_per_mtok=99_000_000,
                output_microcents_per_mtok=99_000_000,
                thoughts_microcents_per_mtok=99_000_000,
            )
        )
        await session.commit()
    book = PriceBook(sessionmaker, refresh_s=0)

    today = await book.price_for(MODEL, now)
    later = await book.price_for(MODEL, now + timedelta(days=31))

    assert today is not None and today.input_microcents_per_mtok == FLASH_INPUT
    assert later is not None and later.input_microcents_per_mtok == 99_000_000


async def test_an_unpriced_model_leaves_the_cost_null(sessionmaker: Sessionmaker) -> None:
    """NULL, not zero: a gap an operator can find, rather than a free-looking request."""
    record = UsageRecord(
        tenant_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        model="some-model-nobody-priced",
        method="generateContent",
        input_tokens=10,
        output_tokens=20,
        status_code=200,
    )

    await PriceBook(sessionmaker, refresh_s=0).apply(record)

    assert record.cost_microcents is None
    assert record.price_id is None


async def test_a_request_with_no_token_counts_is_left_unpriced(
    sessionmaker: Sessionmaker,
) -> None:
    """An upstream timeout reports nothing. Costing it zero would be a lie."""
    record = UsageRecord(
        tenant_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        model=MODEL,
        method="generateContent",
        status_code=504,
    )

    await PriceBook(sessionmaker, refresh_s=0).apply(record)

    assert record.cost_microcents is None


async def test_the_price_book_survives_a_broken_database(sessionmaker: Sessionmaker) -> None:
    """Pricing is not worth losing a ledger row over: the row is written unpriced."""
    book = PriceBook(sessionmaker, refresh_s=0)
    book._sessionmaker = None  # type: ignore[assignment]

    assert await book.price_for(MODEL, datetime.now(UTC)) is None


# --- every ledger row carries a cost ---------------------------------------------------


async def test_a_proxied_request_is_priced(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    response = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    usage = response.json()["usageMetadata"]
    [row] = await usage_rows(sessionmaker)
    assert row.cost_microcents == (
        usage["promptTokenCount"] * FLASH_INPUT // 1_000_000
        + usage["candidatesTokenCount"] * FLASH_OUTPUT // 1_000_000
    )
    assert row.price_id is not None


async def test_a_streamed_request_is_priced(
    live_gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys
) -> None:
    async with live_gateway.stream(
        "POST", STREAM_URL, json=BODY, headers=auth(keys.live)
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    [row] = await usage_rows(sessionmaker)
    assert row.cost_microcents is not None and row.cost_microcents > 0


@pytest.mark.parametrize("path", [URL, STREAM_URL])
async def test_an_upstream_error_is_recorded_but_not_priced(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys, path: str
) -> None:
    body = {"contents": [{"parts": [{"text": "[[mock:error=500]]"}]}]}

    await gateway.post(path, json=body, headers=auth(keys.live))

    [row] = await usage_rows(sessionmaker)
    assert row.error_source == "upstream"
    assert row.cost_microcents is None  # nothing was generated, so nothing is owed


def test_the_dollar_formatter_does_not_go_through_a_float() -> None:
    # 0.1 + 0.2 territory: this is exactly the sum a float would render as 0.300000...4
    assert format_usd(MICROCENTS_PER_USD // 10 + MICROCENTS_PER_USD // 5) == "$0.300000"
