import uuid
from datetime import datetime

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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
