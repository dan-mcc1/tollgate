import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, String, func
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
