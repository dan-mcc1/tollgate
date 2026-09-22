"""The exact tier: one row per normalised request, found by its hash.

Two statements, both deliberately small.

**The lookup is an UPDATE.** Reading the entry and recording that it was read are one
statement rather than a SELECT followed by an UPDATE, so a hit is one round trip and not
two. On the path this tier exists to make fast, a second round trip to Neon would be a
meaningful fraction of the win.

**The store does nothing on conflict.** Two identical requests that miss at the same
moment both go upstream and both come back with an answer to store. First writer wins,
and the loser's response is discarded rather than overwriting the winner's and pushing
the expiry out. An entry therefore ages out at the TTL it was written with, instead of
being kept alive indefinitely by a steady trickle of identical requests - which is how a
cache quietly starts serving a month-old answer.
"""

import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import CacheEntry, Tenant

logger = logging.getLogger("tollgate.cache")


@dataclass(frozen=True)
class CachedResponse:
    """An answer the gateway already had, and what it cost to produce."""

    entry_id: uuid.UUID
    response: dict[str, Any]
    input_tokens: int
    output_tokens: int
    thoughts_tokens: int
    created_at: datetime
    # How close the request was to the one that produced this entry. Always None on the
    # exact tier, where "close" is not a thing a hash can be. The semantic tier fills it.
    similarity: float | None = None

    @property
    def age_s(self) -> float:
        return (datetime.now(UTC) - self.created_at).total_seconds()


async def lookup(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, key: str
) -> CachedResponse | None:
    """This tenant's unexpired entry for `key`, counting the hit as it reads it.

    `expires_at > now()` is evaluated by Postgres rather than by this process, so an
    entry's lifetime is measured on one clock however many containers are asking.
    """
    statement = (
        update(CacheEntry)
        .where(
            CacheEntry.tenant_id == tenant_id,
            CacheEntry.cache_key == key,
            CacheEntry.expires_at > func.now(),
        )
        .values(hits=CacheEntry.hits + 1, last_hit_at=func.now())
        .returning(
            CacheEntry.id,
            CacheEntry.response,
            CacheEntry.input_tokens,
            CacheEntry.output_tokens,
            CacheEntry.thoughts_tokens,
            CacheEntry.created_at,
        )
    )
    async with sessionmaker() as session:
        row = (await session.execute(statement)).one_or_none()
        await session.commit()

    if row is None:
        return None
    return CachedResponse(
        entry_id=row.id,
        response=row.response,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        thoughts_tokens=row.thoughts_tokens,
        created_at=row.created_at,
    )


async def store(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    key: str,
    model: str,
    response: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int,
    ttl_s: float,
    params_key: str | None = None,
    embedding: list[float] | None = None,
) -> None:
    """Keep this answer for `ttl_s` seconds. Silent if an entry already exists.

    The semantic tier's two columns are written here rather than by a second statement,
    so one row is inserted per stored answer. Both are None when that tier is off or
    declined the request, which leaves the entry invisible to the similarity search
    rather than matchable by it on incomplete information.
    """
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "cache_key": key,
        "model": model,
        "response": response,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thoughts_tokens": thoughts_tokens,
        "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_s),
    }
    # Written only together. An entry with a vector but no parameters hash could be
    # matched on its prompt alone, which is the one thing the semantic tier must not do.
    if embedding is not None and params_key is not None:
        values["embedding"] = embedding
        values["params_key"] = params_key

    statement = (
        insert(CacheEntry)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[CacheEntry.tenant_id, CacheEntry.cache_key])
    )
    async with sessionmaker() as session:
        await session.execute(statement)
        await session.commit()


async def prune(session: AsyncSession) -> int:
    """Delete expired entries and say how many went. A maintenance sweep, not a hot path.

    Nothing depends on this running: every read already filters on the expiry, so an
    unpruned table returns the same answers and only takes up more room. It exists so
    that room can be reclaimed on purpose rather than never.
    """
    result = await session.execute(delete(CacheEntry).where(CacheEntry.expires_at <= func.now()))
    await session.commit()
    return cast(CursorResult[Any], result).rowcount or 0


async def clear(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Drop one tenant's entries, expired or not.

    Safe by construction: the table holds nothing that cannot be regenerated by making
    the call again, so the cost of being wrong here is money and never correctness.
    """
    result = await session.execute(delete(CacheEntry).where(CacheEntry.tenant_id == tenant_id))
    await session.commit()
    return cast(CursorResult[Any], result).rowcount or 0


@dataclass(frozen=True)
class CacheStats:
    tenant: str
    entries: int
    hits: int
    expired: int


async def stats(session: AsyncSession) -> list[CacheStats]:
    """What the cache is holding, per tenant. Entry counts and hits, never content."""
    live = func.count().filter(CacheEntry.expires_at > func.now())
    expired = func.count().filter(CacheEntry.expires_at <= func.now())
    rows = (
        await session.execute(
            select(
                Tenant.name,
                live,
                func.coalesce(func.sum(CacheEntry.hits), 0),
                expired,
            )
            .join(Tenant, Tenant.id == CacheEntry.tenant_id)
            .group_by(Tenant.name)
            .order_by(Tenant.name)
        )
    ).all()
    return [CacheStats(name, int(n), int(hits), int(gone)) for name, n, hits, gone in rows]


async def safely(coroutine: Coroutine[Any, Any, None], description: str) -> None:
    """Run a cache write, and let it fail without taking the request with it.

    The cache is an optimisation. A failed insert means the next identical request pays
    the upstream again, which is exactly what would have happened without this phase;
    turning that into a 500 would make the gateway less reliable than the thing it is
    meant to make cheaper. Lookups are guarded the same way, in service.py.
    """
    try:
        await coroutine
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logger.warning(
            "cache %s failed", description, extra={"fields": {"error": type(exc).__name__}}
        )
