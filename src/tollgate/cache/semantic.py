"""The semantic tier: answering a request from a similar one rather than an identical one.

This is the part of the cache that can be wrong. An exact hit is justified by the request
being byte-for-byte the same once normalised, which is a fact. A semantic hit is justified
by a number being above a threshold, which is a judgement - and when the judgement is
wrong the caller gets a confident, well-formed answer to a question nobody asked. That is
a correctness bug, not a performance trade, and everything below is arranged around it.

**What similarity is allowed to cover.** The prompt, and nothing else. Every other thing
that changes an answer - system instruction, tool declarations, temperature, response
schema, model, tenant - is hashed into `params_key` and has to match exactly before two
entries are ever compared (see keys.params_key). A conversation carrying anything but
text is refused outright, because the embedding would not see the image and the hash
would not cover it either.

**Where the threshold comes from.** Not from a plausible-looking constant. bench/
cache_sweep.py embeds a labelled set of prompt pairs, sweeps the threshold, and reports
precision against recall; the operating point is the one with no false hits on that set,
with a margin. The tier ships switched off because the right number is a property of the
embedding model rather than of this gateway, and shipping a default for a model nobody
has measured would be exactly the unfounded transfer the sweep exists to prevent.

**The cost of asking.** An embedding call is an upstream round trip, made before anything
can be looked up and paid for whether or not anything is found. A tier that spends 40 ms
and a fraction of a cent to avoid a 120 ms call only pays off above some hit rate, and
`embedding_cost_microcents` on the ledger is what lets that be checked rather than
assumed. The call is given a short timeout for the same reason: an embedding that takes
longer than the generation it was trying to avoid has already lost, so it is abandoned
and the request is reported as a miss.

**Filtered vector search, and why recall is the safe thing to lose.** The HNSW index
answers "nearest overall", while the query wants "nearest among this tenant's entries
with these parameters". Postgres may walk the graph and then discard most of what it
finds, so a true neighbour can be missed when the filter is selective. pgvector 0.8 can
keep scanning until it has enough rows that survive the filter, which is what
`hnsw.iterative_scan` below turns on. Where that still falls short the failure is a miss:
the request goes upstream and the caller gets a correct, freshly generated answer. Recall
lost costs money; precision lost costs trust, and only one of those is recoverable.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import Float, and_, cast, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.cache.exact import CachedResponse
from tollgate.db.models import CacheEntry

logger = logging.getLogger("tollgate.cache")


@dataclass(frozen=True)
class Embedding:
    """A prompt as a vector, and what the provider charged to produce it."""

    values: list[float]
    tokens: int


class Embedder:
    """Turns a prompt into a vector, using the same provider the gateway proxies.

    The gateway's own HTTP client is reused, so the embedding call shares the connection
    pool, the provider credential and the timeouts that everything else already has. The
    timeout here is deliberately tighter than the one on a generation call: this request
    is pure overhead on the way to an answer, and one that runs long has already cost
    more than the call it hoped to save.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        model: str,
        dimensions: int,
        timeout_s: float,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self._timeout_s = timeout_s

    async def embed(self, prompt: str) -> Embedding | None:
        """The prompt as a vector, or None if the provider could not be asked.

        None is never an error the caller has to handle: it means the semantic tier did
        not get to run, the request is a miss, and it goes upstream exactly as it would
        have without this tier existing.
        """
        try:
            response = await self._client.post(
                f"/v1beta/models/{self.model}:embedContent",
                json={
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": prompt}]},
                    "outputDimensionality": self.dimensions,
                    # The provider optimises the vector for being searched rather than
                    # for being stored. Both sides of this comparison are questions, so
                    # both are embedded the same way; using the query and document task
                    # types on the two sides would put them in subtly different spaces.
                    "taskType": "SEMANTIC_SIMILARITY",
                },
                headers={"x-goog-api-key": self._api_key},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("embedding unavailable", extra={"fields": {"error": type(exc).__name__}})
            return None

        values = (payload.get("embedding") or {}).get("values")
        if not isinstance(values, list) or len(values) != self.dimensions:
            logger.warning(
                "embedding was not the shape expected",
                extra={"fields": {"expected": self.dimensions, "got": _length(values)}},
            )
            return None
        # Token counts are not in the embedContent response, so the prompt is charged by
        # the same four-characters-to-a-token rule the budget estimate uses. It is an
        # estimate of a cost that is two orders of magnitude below the generation call
        # it is trying to avoid; being a little wrong about it changes no decision.
        return Embedding([float(value) for value in values], tokens=max(1, len(prompt) // 4))


def _length(value: Any) -> int:
    return len(value) if isinstance(value, list) else -1


async def nearest(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    params_key: str,
    embedding: list[float],
    threshold: float,
    ef_search: int,
) -> CachedResponse | None:
    """This tenant's closest entry above `threshold`, or None.

    One statement, ordered by cosine distance and cut off at the threshold, so the
    decision is made by Postgres and this process never sees the entries that were not
    close enough. `1 - (embedding <=> query)` is cosine similarity: `<=>` is cosine
    *distance*, and the threshold is expressed as similarity because that is the number
    the sweep produces and the number a person can reason about.
    """
    similarity = 1 - CacheEntry.embedding.cosine_distance(embedding)
    statement = (
        select(
            CacheEntry.id,
            CacheEntry.response,
            CacheEntry.input_tokens,
            CacheEntry.output_tokens,
            CacheEntry.thoughts_tokens,
            CacheEntry.created_at,
            cast(similarity, Float).label("similarity"),
        )
        .where(
            CacheEntry.tenant_id == tenant_id,
            CacheEntry.params_key == params_key,
            CacheEntry.embedding.is_not(None),
            CacheEntry.expires_at > func.now(),
            similarity >= threshold,
        )
        .order_by(CacheEntry.embedding.cosine_distance(embedding))
        .limit(1)
    )

    async with sessionmaker() as session:
        # Per-transaction, so one slow lookup cannot change how hard every later one
        # works. `iterative_scan` is what keeps a filtered search from coming back empty
        # merely because the nearest rows overall belonged to another tenant.
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        # Counted the same way an exact hit is, so `hits` means the same thing on every
        # entry however it was found.
        await session.execute(
            update(CacheEntry)
            .where(and_(CacheEntry.id == row.id))
            .values(hits=CacheEntry.hits + 1, last_hit_at=func.now())
        )
        await session.commit()

    return CachedResponse(
        entry_id=row.id,
        response=row.response,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        thoughts_tokens=row.thoughts_tokens,
        created_at=row.created_at,
        similarity=float(row.similarity),
    )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity between two vectors, for the bench and the tests.

    The same measure the index uses, computed here so bench/cache_sweep.py can sweep a
    threshold without a database in the loop - the sweep is about the embedding model
    and the number, not about how the rows are stored.
    """
    if len(left) != len(right):
        raise ValueError("vectors of different lengths are not comparable")
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)
