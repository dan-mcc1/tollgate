"""Usage accounting: token counts, what they cost, and writing the ledger.

Money here is always an integer number of **micro-cents**: one millionth of a cent,
1e-8 USD. Never a float, and never a cent.

Floats are out because 0.30 has no exact binary representation, so a ledger built on
them drifts by amounts too small to notice and too late to reconstruct. Cents are out
because a cent cannot hold a request: one flash call costs a few hundred micro-cents, so
a ledger denominated in cents rounds every row to zero and a busy month sums to nothing.
Micro-cents hold the smallest thing being charged for, and 64 bits of them reach nine
figures of dollars, so the arithmetic stays exact from a single token to any bill this
gateway will ever produce.
"""

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import ModelPrice, UsageRecord

logger = logging.getLogger("tollgate.usage")

MICROCENTS_PER_CENT = 1_000_000
MICROCENTS_PER_USD = 100 * MICROCENTS_PER_CENT
TOKENS_PER_MTOK = 1_000_000

# Work that outlived the request that started it (a client that hung up mid-stream).
# Kept referenced so the event loop can't garbage-collect a task half way through.
_background: set[asyncio.Task[None]] = set()


# --- reading token counts off the wire ------------------------------------------------


def apply_usage_payload(record: UsageRecord, payload: dict[str, Any]) -> None:
    """Copy token counts from one parsed Gemini response (or stream event) onto the record.

    A streamed response repeats usageMetadata on each event with running totals, so the
    last event seen wins. Counts stay NULL when the upstream never reported any, which is
    different from reporting zero.
    """
    usage = payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return
    record.input_tokens = usage.get("promptTokenCount")
    record.output_tokens = usage.get("candidatesTokenCount")
    record.thoughts_tokens = usage.get("thoughtsTokenCount")


def apply_usage_metadata(record: UsageRecord, body: bytes) -> None:
    """As above, for a complete (non-streamed) response body."""
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return
    if isinstance(payload, dict):
        apply_usage_payload(record, payload)


def upstream_error_status(payload: dict[str, Any]) -> str | None:
    """The error status inside one parsed response or stream event, if it carries one.

    A provider can fail *inside* a stream: the connection stays healthy and an ordinary
    event arrives holding an error object instead of content. Nothing else notices, so
    without this the ledger would record a failed request as a clean success.
    """
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    status = error.get("status") or error.get("code")
    return str(status) if status is not None else "unknown"


def upstream_error_code(body: bytes) -> str | None:
    """The `status` field of a Google-shaped error body, e.g. RESOURCE_EXHAUSTED."""
    try:
        payload: Any = json.loads(body)
        status = payload["error"]["status"]
    except (ValueError, KeyError, TypeError):
        return None
    return status if isinstance(status, str) else None


# --- what those tokens cost -----------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """One version of one model's price, as the gateway charges it."""

    id: uuid.UUID
    effective_from: datetime
    input_microcents_per_mtok: int
    output_microcents_per_mtok: int
    thoughts_microcents_per_mtok: int

    @classmethod
    def from_row(cls, row: ModelPrice) -> "Price":
        return cls(
            id=row.id,
            effective_from=row.effective_from,
            input_microcents_per_mtok=row.input_microcents_per_mtok,
            output_microcents_per_mtok=row.output_microcents_per_mtok,
            thoughts_microcents_per_mtok=row.thoughts_microcents_per_mtok,
        )

    def cost(self, *, input_tokens: int, output_tokens: int, thoughts_tokens: int) -> int:
        """What these token counts cost, in micro-cents. Integers throughout.

        Each component is floored on its own, so one request is under-charged by at most
        two micro-cents (2e-8 USD) - an error that rounds towards the tenant, which is
        the direction to be wrong in. It cannot accumulate: the ledger stores this
        number and not the rate, so a month is a sum of exact integers.
        """
        return (
            input_tokens * self.input_microcents_per_mtok // TOKENS_PER_MTOK
            + output_tokens * self.output_microcents_per_mtok // TOKENS_PER_MTOK
            + thoughts_tokens * self.thoughts_microcents_per_mtok // TOKENS_PER_MTOK
        )


def format_usd(microcents: int) -> str:
    """Micro-cents as a dollar string, for people. Decimal, so nothing rounds twice."""
    return f"${Decimal(microcents) / MICROCENTS_PER_USD:.6f}"


class PriceBook:
    """The price table, held in this process.

    Every request needs a price and the table changes a few times a year, so reading it
    per request would be a database round trip for a value that almost never moves. The
    cache is refreshed on a timer rather than invalidated, because a price inserted by
    another container has no way to reach this one; `refresh_s` is therefore the longest
    a new price can take to come into force across the fleet.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], refresh_s: float) -> None:
        self._sessionmaker = sessionmaker
        self._refresh_s = refresh_s
        self._prices: dict[str, list[Price]] = {}
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._loaded_at is not None and time.monotonic() - self._loaded_at < self._refresh_s

    async def _load(self) -> dict[str, list[Price]]:
        if self._fresh():
            return self._prices
        async with self._lock:
            # Another request may have refreshed the table while this one queued here.
            if self._fresh():
                return self._prices
            async with self._sessionmaker() as session:
                rows = await session.scalars(
                    select(ModelPrice).order_by(ModelPrice.model, ModelPrice.effective_from.desc())
                )
                prices: dict[str, list[Price]] = defaultdict(list)
                for row in rows:
                    prices[row.model].append(Price.from_row(row))  # newest first
            self._prices, self._loaded_at = dict(prices), time.monotonic()
            return self._prices

    async def price_for(self, model: str, at: datetime) -> Price | None:
        """The price in force for `model` at `at`, or None if the model has no price."""
        try:
            prices = await self._load()
        except Exception:
            # Pricing is not worth losing a ledger row over. Carry on with whatever was
            # last loaded, and let the row be written unpriced rather than not at all.
            logger.exception("could not load the price table")
            prices = self._prices
        return next((price for price in prices.get(model, ()) if price.effective_from <= at), None)

    async def apply(self, record: UsageRecord, at: datetime | None = None) -> None:
        """Set `cost_microcents` and `price_id` on a record about to be written.

        Both stay NULL when the upstream never reported tokens, or when the model has no
        price. That is deliberate: an unpriced row is a gap a query can find and an
        operator can fill, where a zero is indistinguishable from a genuinely free call.
        """
        if record.input_tokens is None and record.output_tokens is None:
            return
        price = await self.price_for(record.model, at or datetime.now(UTC))
        if price is None:
            logger.warning("no price for model", extra={"fields": {"model": record.model}})
            return
        record.price_id = price.id
        record.cost_microcents = price.cost(
            input_tokens=record.input_tokens or 0,
            output_tokens=record.output_tokens or 0,
            thoughts_tokens=record.thoughts_tokens or 0,
        )


# --- writing the ledger ---------------------------------------------------------------


async def write_usage(
    sessionmaker: async_sessionmaker[AsyncSession], pricebook: PriceBook, record: UsageRecord
) -> None:
    """Price the request, then append it to the ledger.

    Pricing happens here rather than at the call sites, so that no path through the
    proxy - success, upstream error, timeout, abandoned stream - can write a row that
    nobody costed.
    """
    await pricebook.apply(record)
    async with sessionmaker() as session:
        session.add(record)
        await session.commit()


async def run_even_if_cancelled(work: Coroutine[Any, Any, None], description: str) -> None:
    """Run `work` to completion even though the caller is being cancelled.

    A client hanging up mid-stream cancels the task doing the relaying, and a plain
    `await` inside that task would be cancelled with it - losing a row for tokens the
    provider has already generated and billed, and leaving that request's budget
    reservation held until its lease runs out. The work runs as its own task, shielded,
    so cancellation stops the waiting rather than the doing.
    """
    task = asyncio.create_task(work)
    _background.add(task)
    task.add_done_callback(lambda done: _finish_background(done, description))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        pass  # the shielded task keeps running; the caller's cancellation continues below
    except Exception:
        pass  # the callback below is the one place this is reported


def _finish_background(task: asyncio.Task[None], description: str) -> None:
    _background.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("%s lost", description, exc_info=error)


# --- reading the ledger back --------------------------------------------------------------

MONTH_FORMAT = "%Y-%m"


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """The half-open UTC range covering a "YYYY-MM" string.

    Half-open on purpose: `created_at < end` has no gap and no overlap with the next
    month, where `<=` on the last microsecond of the month would eventually lose a row
    to whichever side rounded differently.
    """
    start = datetime.strptime(month, MONTH_FORMAT).replace(tzinfo=UTC)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


@dataclass(frozen=True)
class ModelSpend:
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    thoughts_tokens: int
    cost_microcents: int


@dataclass(frozen=True)
class MonthlySpend:
    month: str
    requests: int
    cost_microcents: int
    by_model: list[ModelSpend]


async def rollup(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, month: str
) -> MonthlySpend:
    """One tenant's month, grouped by model.

    The ledger is append-only, so this is a scan over a range of one tenant's rows and
    nothing else: there are no updates to race with and no soft-deleted rows to filter
    out. What makes it cheap is the index on (tenant_id, created_at), which is what
    turns "every row this gateway ever wrote" into "this tenant's rows this month".

    Rows with a NULL cost - an upstream timeout, an unpriced model - contribute zero
    rather than being dropped, so the request counts still add up.
    """
    start, end = month_bounds(month)
    stmt = (
        select(
            UsageRecord.model,
            func.count().label("requests"),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageRecord.thoughts_tokens), 0).label("thoughts_tokens"),
            func.coalesce(func.sum(UsageRecord.cost_microcents), 0).label("cost"),
        )
        .where(
            UsageRecord.tenant_id == tenant_id,
            UsageRecord.created_at >= start,
            UsageRecord.created_at < end,
        )
        .group_by(UsageRecord.model)
        .order_by(func.coalesce(func.sum(UsageRecord.cost_microcents), 0).desc())
    )
    async with sessionmaker() as session:
        rows = (await session.execute(stmt)).all()

    by_model = [
        ModelSpend(
            model=row.model,
            requests=row.requests,
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
            thoughts_tokens=int(row.thoughts_tokens),
            cost_microcents=int(row.cost),
        )
        for row in rows
    ]
    return MonthlySpend(
        month=month,
        requests=sum(entry.requests for entry in by_model),
        cost_microcents=sum(entry.cost_microcents for entry in by_model),
        by_model=by_model,
    )
