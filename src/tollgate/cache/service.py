"""The cache as the proxy sees it: ask before the upstream, offer after it.

Two calls, and everything else in this package is behind them.

    lookup = await cache.lookup(tenant, model, query, body)
    if lookup.entry is not None:     # serve it
    ...
    await cache.store(lookup, response_payload)

**Two tiers, tried in order.** The exact tier is one indexed read on a hash, so it is
tried first and costs almost nothing when it misses. The semantic tier costs an embedding
call - an upstream round trip, paid whether or not anything is found - so it only runs
once the exact tier has already failed, and only when it is switched on. A request that
hits exactly never pays for an embedding.

**A lookup never raises.** The cache is an optimisation on top of a gateway that worked
without it. A failed lookup is reported as a miss, the request goes upstream, and the
caller gets exactly what it would have got in phase five. The alternative - a database
hiccup in the cache turning into a 500 - would make the gateway less reliable than the
thing it is meant to make cheaper.

**Where this sits in the request.** After authentication, the rate limit and the budget
reservation; before the upstream call. That order is deliberate and costs some hits: a
tenant at its monthly cap is refused even for a question the gateway could have answered
for free. It is kept because the alternative is a budget whose enforcement depends on
what happens to be cached, which is impossible to explain to whoever is being billed -
and because the semantic tier spends real money on an embedding before it can find
anything, which a capped tenant should not be able to trigger.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.cache import exact, semantic
from tollgate.cache.exact import CachedResponse
from tollgate.cache.keys import (
    BYPASS,
    EXACT_HIT,
    MISS,
    SEMANTIC_HIT,
    cache_key,
    params_key,
    prompt_text,
    query_material,
    request_block_reason,
    request_payload,
    response_block_reason,
    semantic_block_reason,
)
from tollgate.cache.replay import token_counts
from tollgate.cache.semantic import Embedder, Embedding
from tollgate.config import Settings
from tollgate.db.models import EMBEDDING_DIMENSIONS
from tollgate.telemetry import facts, stage

logger = logging.getLogger("tollgate.cache")


@dataclass(frozen=True)
class Lookup:
    """What the cache did when it was asked, and what `store` needs to finish the job.

    `key` is None whenever the request was turned away, which is what makes a later
    `store` a no-op without the call site having to remember why.
    """

    status: str | None  # "exact", "semantic", "miss", "bypass", or None when off
    key: str | None = None
    entry: CachedResponse | None = None
    reason: str | None = None  # why it was bypassed, for the span and the log
    # Carried from the lookup to the store so the vector is computed once per request.
    # The embedding made to search with is the same one the new entry is filed under.
    params: str | None = None
    embedding: Embedding | None = None
    # What the embedding call cost, in tokens. Paid on a semantic miss as much as on a
    # semantic hit, which is the number that decides whether the tier is worth running.
    embedding_tokens: int = 0

    @property
    def is_hit(self) -> bool:
        return self.entry is not None

    @property
    def similarity(self) -> float | None:
        return self.entry.similarity if self.entry is not None else None


DISABLED = Lookup(status=None)


class CacheService:
    """Reads and writes the response cache for one gateway process."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        enabled: bool,
        ttl_s: float,
        max_temperature: float,
        assumed_temperature: float,
        max_response_bytes: int,
        embedder: Embedder | None = None,
        semantic_threshold: float | None = None,
        ef_search: int = 100,
    ) -> None:
        self._sessionmaker = sessionmaker
        self.enabled = enabled
        self._ttl_s = ttl_s
        self._max_temperature = max_temperature
        self._assumed_temperature = assumed_temperature
        # How much of a streamed response may be held in order to cache it. See
        # proxy/streaming.py: the phase 3 promise is that memory stays flat under a long
        # response, and a bound is precisely what keeps that promise true.
        self.max_response_bytes = max_response_bytes
        # None means the semantic tier is off, which is the shipped default. There is
        # deliberately no separate boolean: the tier is on exactly when it has something
        # to embed with, so it cannot be half-configured.
        self._embedder = embedder
        if embedder is not None and semantic_threshold is None:
            # The invariant lives here as well as in build_cache, so a service put
            # together directly - by a test, or a future caller - cannot end up with an
            # embedder and no cut-off to apply to what it finds.
            raise ValueError("a semantic embedder needs a threshold; see cache_sweep.py")
        self._threshold = semantic_threshold
        self._ef_search = ef_search

    @property
    def semantic_enabled(self) -> bool:
        return self._embedder is not None

    @property
    def embedding_model(self) -> str | None:
        """The model an embedding is charged at, or None when the tier is off."""
        return self._embedder.model if self._embedder is not None else None

    async def lookup(
        self,
        *,
        tenant_id: uuid.UUID,
        model: str,
        query_items: list[tuple[str, str]],
        body: bytes,
    ) -> Lookup:
        """Ask the cache about this request. Returns a miss rather than raising, always."""
        if not self.enabled:
            return DISABLED

        payload = request_payload(body)
        reason = request_block_reason(
            payload,
            max_temperature=self._max_temperature,
            assumed_temperature=self._assumed_temperature,
        )
        if reason is not None or payload is None:
            return Lookup(status=BYPASS, reason=reason or "unparseable_body")

        query = query_material(query_items)
        key = cache_key(tenant_id=tenant_id, model=model, payload=payload, query=query or "")
        if key is None or query is None:
            return Lookup(status=BYPASS, reason="uncanonicalisable_body")

        with stage("cache.lookup") as span:
            entry = await self._exact(tenant_id, key)
            if entry is not None:
                span.set_attribute("tollgate.cache.status", EXACT_HIT)
                span.set_attribute("tollgate.cache.age_s", round(entry.age_s, 3))
                return Lookup(status=EXACT_HIT, key=key, entry=entry)
            span.set_attribute("tollgate.cache.status", MISS)

        return await self._semantic(tenant_id, model, payload, query, key)

    async def _semantic(
        self,
        tenant_id: uuid.UUID,
        model: str,
        payload: dict[str, Any],
        query: str,
        key: str,
    ) -> Lookup:
        """The second tier, after the first has missed.

        Everything that can decline is checked before the embedding call, because the
        embedding is the expensive part and there is no sense paying for it to find out
        that this request was never eligible.
        """
        miss = Lookup(status=MISS, key=key)
        if self._embedder is None:
            return miss

        params = params_key(tenant_id=tenant_id, model=model, payload=payload, query=query)
        reason = semantic_block_reason(payload)
        if params is None or reason is not None:
            # Still a miss and not a bypass: the exact tier looked and will store the
            # answer. It is only the similarity search that cannot run.
            logger.debug("semantic tier skipped", extra={"fields": {"reason": reason}})
            return miss

        started = time.perf_counter()
        with stage("cache.embed") as span:
            embedding = await self._embedder.embed(prompt_text(payload))
            span.set_attribute("tollgate.cache.embedded", embedding is not None)
        # Recorded whether or not an embedding came back, because the waiting happened
        # either way. This is provider time, and telemetry.emit_request_metrics counts
        # it as such rather than letting it land in the gateway's own overhead.
        facts().embedding_ms += (time.perf_counter() - started) * 1000
        if embedding is None:
            return miss

        found = Lookup(
            status=MISS,
            key=key,
            params=params,
            embedding=embedding,
            embedding_tokens=embedding.tokens,
        )
        with stage("cache.semantic") as span:
            entry = await self._nearest(tenant_id, params, embedding)
            span.set_attribute("tollgate.cache.status", SEMANTIC_HIT if entry else MISS)
            if entry is not None and entry.similarity is not None:
                span.set_attribute("tollgate.cache.similarity", round(entry.similarity, 5))
        if entry is None:
            return found
        return Lookup(
            status=SEMANTIC_HIT,
            key=key,
            entry=entry,
            params=params,
            embedding=embedding,
            embedding_tokens=embedding.tokens,
        )

    async def _exact(self, tenant_id: uuid.UUID, key: str) -> CachedResponse | None:
        try:
            return await exact.lookup(self._sessionmaker, tenant_id, key)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logger.warning("cache lookup failed", extra={"fields": {"error": type(exc).__name__}})
            return None

    async def _nearest(
        self, tenant_id: uuid.UUID, params: str, embedding: Embedding
    ) -> CachedResponse | None:
        # Guaranteed by __init__: this is only reached with an embedder in hand, and an
        # embedder cannot be installed without a threshold to go with it.
        assert self._threshold is not None
        try:
            return await semantic.nearest(
                self._sessionmaker,
                tenant_id=tenant_id,
                params_key=params,
                embedding=embedding.values,
                threshold=self._threshold,
                ef_search=self._ef_search,
            )
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logger.warning(
                "semantic lookup failed", extra={"fields": {"error": type(exc).__name__}}
            )
            return None

    async def store(
        self,
        lookup: Lookup,
        *,
        tenant_id: uuid.UUID,
        model: str,
        response: dict[str, Any] | None,
    ) -> str | None:
        """Offer a fresh response to the cache. Returns why it was refused, or None.

        A no-op unless the request missed - a hit has nothing to add, and a bypassed
        request was never eligible - so the call sites do not have to check first.

        The vector written here is the one the lookup already paid for, so a miss costs
        one embedding call rather than two. When the semantic tier is off, or declined
        this request, both `params_key` and `embedding` are NULL and the entry is simply
        invisible to the similarity search rather than wrongly matched by it.
        """
        if lookup.status != MISS or lookup.key is None:
            return lookup.reason
        reason = response_block_reason(response)
        if reason is not None or response is None:
            return reason or "unparseable_response"

        input_tokens, output_tokens, thoughts_tokens = token_counts(response)
        with stage("cache.store"):
            await exact.safely(
                exact.store(
                    self._sessionmaker,
                    tenant_id=tenant_id,
                    key=lookup.key,
                    model=model,
                    response=response,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    thoughts_tokens=thoughts_tokens,
                    ttl_s=self._ttl_s,
                    params_key=lookup.params,
                    embedding=lookup.embedding.values if lookup.embedding else None,
                ),
                "store",
            )
        return None


def build_cache(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient | None = None,
) -> CacheService:
    """The cache this process will use.

    The semantic tier needs the gateway's HTTP client to reach the provider, so it is
    only ever built where that client exists. Without one the tier is off, which is also
    what every test that has not asked for it gets.
    """
    embedder = None
    if settings.cache_semantic_enabled and http_client is not None:
        if settings.cache_semantic_threshold is None:
            # Refused rather than defaulted. A threshold belongs to the embedding model
            # it was swept against, and the two sweeps in this repository disagree about
            # it entirely, so any value shipped here would be a number derived from a
            # model the operator is probably not using. Failing to start is the honest
            # outcome; the alternative - a plausible default - is how a correctness bug
            # gets configured in by somebody who never knew there was a choice.
            raise RuntimeError(
                "CACHE_SEMANTIC_ENABLED=True needs CACHE_SEMANTIC_THRESHOLD. "
                "Run bench/cache_sweep.py against the embeddings you will use: it prints "
                "the value to set, or reports that no safe value exists for that model."
            )
        if settings.cache_embedding_dimensions != EMBEDDING_DIMENSIONS:
            # Caught at startup rather than as a width error on the first insert: the
            # column is a fixed width, so disagreeing with it is a migration and not a
            # setting, and failing here says so.
            raise RuntimeError(
                "CACHE_EMBEDDING_DIMENSIONS must match the width of the embedding column "
                f"({EMBEDDING_DIMENSIONS}); changing it is a migration."
            )
        embedder = Embedder(
            http_client,
            settings.gemini_api_key.get_secret_value(),
            model=settings.cache_embedding_model,
            dimensions=settings.cache_embedding_dimensions,
            timeout_s=settings.cache_embedding_timeout_s,
        )
    return CacheService(
        sessionmaker,
        enabled=settings.cache_enabled,
        ttl_s=settings.cache_ttl_s,
        max_temperature=settings.cache_max_temperature,
        assumed_temperature=settings.cache_assumed_temperature,
        max_response_bytes=settings.cache_max_response_bytes,
        embedder=embedder,
        semantic_threshold=settings.cache_semantic_threshold,
        ef_search=settings.cache_hnsw_ef_search,
    )
