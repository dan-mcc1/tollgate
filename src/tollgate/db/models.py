import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Fixed in the schema, because a column's width cannot follow a setting: changing it is a
# migration and a re-embedding of every row, not a restart. 768 rather than the 3072 the
# model can produce - pgvector indexes up to 2000 dimensions, the shorter output is a
# supported truncation rather than a lesser model, and it is a quarter of the storage and
# of the arithmetic in every distance comparison. `cache_embedding_dimensions` must agree.
EMBEDDING_DIMENSIONS = 768

# Deterministic constraint names, so autogenerate diffs stay stable across databases.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    # Sustained requests per minute, and how many may arrive at once. NULL rpm means
    # unlimited, which is how an internal tenant or a load generator is exempted
    # without a magic number; a NULL burst is one minute's worth. Every tenant gets a
    # limit by default, because a gateway whose limits are opt-in has none.
    rate_limit_rpm: Mapped[int | None] = mapped_column(server_default="60")
    rate_limit_burst: Mapped[int | None] = mapped_column(server_default="60")
    # Monthly spend cap in micro-cents. NULL means unlimited, and unlike the rate limit
    # above that is the default: a rate limit is a technical cap with a sane generic
    # value, while a budget is what a customer agreed to pay. There is no honest number
    # to invent for that, and inventing one cuts a paying tenant off mid-month.
    monthly_budget_microcents: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="tenant")


class ApiKey(Base):
    """A tenant credential. Only a SHA-256 hash of the key is stored; the prefix
    is kept in plaintext so a key can be identified in logs and dashboards."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


class UsageRecord(Base):
    """One row per authenticated request that the gateway tried to send upstream.

    Rows are never updated or deleted. Token counts are NULL when the upstream never
    reported them (a timeout, an upstream error), which is different from zero.
    """

    __tablename__ = "usage_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # No ondelete cascade: a tenant with usage history can't be deleted, only deactivated.
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    model: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    thoughts_tokens: Mapped[int | None]
    # What the request cost, in micro-cents, at the price version pinned beside it.
    # NULL when the upstream never reported tokens, or the model has no price row:
    # a gap can be found and filled later, whereas a zero reads as a free request.
    cost_microcents: Mapped[int | None] = mapped_column(BigInteger)
    price_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_prices.id"))
    upstream_latency_ms: Mapped[int | None]
    # Time to the first byte from the upstream. On a stream that's the first token, which
    # is what a user actually waits for; the rest arrives while they read.
    upstream_ttfb_ms: Mapped[int | None]
    upstream_attempts: Mapped[int] = mapped_column(default=0)
    status_code: Mapped[int]  # the status Tollgate returned to the caller
    error_source: Mapped[str | None] = mapped_column(String(16))  # "gateway" or "upstream"
    error_code: Mapped[str | None] = mapped_column(String(64))
    streamed: Mapped[bool] = mapped_column(default=False, server_default="false")
    # The caller hung up mid-stream. The tokens were still generated and still cost money,
    # so the row exists and counts what the upstream had reported by then.
    client_disconnected: Mapped[bool] = mapped_column(default=False, server_default="false")
    # What the cache did with this request: "exact", "semantic", "miss" or "bypass".
    # NULL means caching was switched off, which is different from a request that was
    # offered to the cache and turned away.
    cache_status: Mapped[str | None] = mapped_column(String(16))
    # How close the request was to the entry that answered it, as cosine similarity.
    # Set only on a semantic hit, and kept because it is the evidence: a false hit found
    # later is investigated by asking what score let it through.
    cache_similarity: Mapped[float | None]
    # What was spent embedding this request, in micro-cents. The semantic tier calls the
    # provider before it can look anything up, so a miss under it costs money that a miss
    # without it does not. Recorded separately from `cost_microcents` because it is the
    # gateway's own spend on the tenant's behalf, and netting it against the saving is
    # the only honest way to say whether the tier pays for itself.
    embedding_cost_microcents: Mapped[int | None] = mapped_column(BigInteger)
    # What this request would have cost had it gone upstream, priced the same way a real
    # charge is. Set only on a hit, where `cost_microcents` is zero because the tenant is
    # not charged for an answer the gateway already had. The two columns are kept apart
    # on purpose: adding a notional saving into the ledger's money column would inflate
    # every bill and every budget check that reads it.
    cost_avoided_microcents: Mapped[int | None] = mapped_column(BigInteger)

    # Every read of this table is "one tenant, one range of time": the budget check on
    # each request, and the spend rollup behind /v1/spend. Leading with tenant_id narrows
    # to one tenant, then created_at makes the month a contiguous range rather than a
    # filter applied to the whole table.
    #
    # INCLUDE carries the columns those two queries read, which is what turns a bitmap
    # heap scan into an index-only scan. It matters more than it looks: a tenant's rows
    # are scattered through the table, because the ledger is written in arrival order
    # and not grouped by tenant, so without it Postgres visits close to one heap block
    # per row. Measured on 400k rows it is 1.1 ms against 0.2 ms, and 2508 buffers
    # against 33; the index costs about twice the space and a little more work per
    # insert. bench/rollup_plan.py prints all three plans.
    __table_args__ = (
        Index(
            "ix_usage_records_tenant_id_created_at",
            "tenant_id",
            "created_at",
            postgresql_include=[
                "model",
                "input_tokens",
                "output_tokens",
                "thoughts_tokens",
                "cost_microcents",
            ],
        ),
    )


class ModelPrice(Base):
    """What one model costs per million tokens, from a moment in time onward.

    Prices are never edited in place. A change is a new row with a later
    `effective_from`, and every usage row pins the `price_id` it was charged at, so a
    price correction made in March cannot quietly rewrite what a tenant owed in January.

    Amounts are integer micro-cents (one millionth of a cent, 1e-8 USD) per million
    tokens. A cent is far too coarse a unit to hold a request: a typical flash call
    costs a few hundred micro-cents, so a ledger denominated in cents would round every
    single row to zero and the month would sum to nothing.
    """

    __tablename__ = "model_prices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model: Mapped[str] = mapped_column(String(200))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_microcents_per_mtok: Mapped[int] = mapped_column(BigInteger)
    output_microcents_per_mtok: Mapped[int] = mapped_column(BigInteger)
    # Gemini bills thinking tokens at the output rate. It is a column rather than a
    # branch in the pricing code, so a provider that charges differently is one row.
    thoughts_microcents_per_mtok: Mapped[int] = mapped_column(BigInteger)

    # The unique constraint's index is also the lookup index: finding the price in
    # force is "this model, greatest effective_from not after the request", which
    # reads backwards along (model, effective_from). No second index is needed.
    __table_args__ = (UniqueConstraint("model", "effective_from"),)


class CacheEntry(Base):
    """One answer the gateway already has, kept so the provider is not asked twice.

    Unlike `usage_records` this table is disposable. Losing all of it costs money and no
    correctness: every row can be regenerated by making the call again, which is why it
    may be truncated freely and why the foreign key cascades where the ledger's does not.

    **No prompt text is stored.** The request is present only as `cache_key`, a SHA-256
    over its normalised form, which cannot be read back. The response is stored whole,
    because the response is the thing being cached - but a dump of this table does not
    reveal what anybody asked.

    **Scoped to one tenant, twice over.** `tenant_id` is a column, so the lookup filters
    on it, and it is also inside the hash, so the keys of two tenants asking the same
    question do not collide even if a query one day forgets the filter. See
    cache/keys.py and the threat model in the README.
    """

    __tablename__ = "cache_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    cache_key: Mapped[str] = mapped_column(String(64))
    # Everything about the request except the prompt. The semantic tier compares prompts
    # and only prompts, so this is what must match exactly before two entries are ever
    # considered similar: same system instruction, tools, temperature, schema, model.
    # NULL on an entry written by a request the semantic tier would not serve anyway.
    params_key: Mapped[str | None] = mapped_column(String(64))
    # The prompt, as the embedding model saw it. NULL when the tier is switched off, so
    # entries written while it was off are invisible to it rather than wrongly matched.
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    model: Mapped[str] = mapped_column(String(200))
    # The provider's own response object, as a hit replays it. JSONB rather than text so
    # a query can reach inside it - reporting on finish reasons, or finding every entry
    # holding a given model version - without the reader having to parse it first.
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # What the upstream reported when this answer was generated. Kept on the entry so a
    # hit can be priced at today's rates, rather than storing a cost that a price change
    # would silently make wrong.
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    thoughts_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(default=0, server_default="0")
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The exact lookup is "this tenant, this key, not yet expired", so the unique
    # constraint's index answers it on its own and no second index is needed.
    #
    # `expires_at` is deliberately *not* indexed. The only query that leads with it is the
    # prune sweep, which is a rare maintenance pass over the whole table and is happy with
    # a sequential scan; an index on it would instead be written on every insert, which is
    # the hot path. Expired rows in the meantime are invisible rather than wrong, because
    # every read filters on the expiry.
    #
    # The vector index is HNSW rather than IVFFlat. IVFFlat has to be built against rows
    # that already exist - it partitions the space by clustering a sample, so an index
    # built on an empty table sorts everything into one list and has to be rebuilt once
    # there is data, which for a cache that starts empty on every deploy is the wrong
    # shape entirely. HNSW builds a navigable graph incrementally, so it is correct from
    # the first row and stays correct as rows arrive. It costs more to build and more
    # memory to hold; at the size a response cache reaches, that is the cheaper mistake.
    #
    # `vector_cosine_ops` because the embeddings are normalised and cosine is the measure
    # the threshold in bench/cache_sweep.py is swept over. An index built for a different
    # operator is simply not used by a cosine query - silently, and with no error.
    __table_args__ = (
        UniqueConstraint("tenant_id", "cache_key"),
        Index(
            "ix_cache_entries_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
