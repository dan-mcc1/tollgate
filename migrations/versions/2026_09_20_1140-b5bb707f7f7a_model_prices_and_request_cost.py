"""model_prices, and the cost of each request

Creates the versioned price table and pins each usage row to the price version it was
charged at. Seeds the prices in force at the time of writing; a later change is a new
row with a later effective_from, never an edit to these.

Revision ID: b5bb707f7f7a
Revises: 09625236705e
Create Date: 2026-09-20 11:40:12.884301

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5bb707f7f7a"
down_revision: str | Sequence[str] | None = "09625236705e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Published list prices per million tokens, as micro-cents (USD * 100_000_000).
# Thinking tokens bill at the output rate on this provider.
USD_PER_MTOK = {
    "gemini-3.7-flash": ("0.30", "2.50"),
    "gemini-3.7-pro": ("1.25", "10.00"),
    "gemini-embedding-001": ("0.15", "0"),
}
# Fixed ids, so the same price version has the same id in every environment and a
# usage row can be traced to a price across a dump, a restore or a rebuild.
PRICE_IDS = {
    "gemini-3.7-flash": uuid.UUID("9a5b2c10-0000-4000-8000-000000000001"),
    "gemini-3.7-pro": uuid.UUID("9a5b2c10-0000-4000-8000-000000000002"),
    "gemini-embedding-001": uuid.UUID("9a5b2c10-0000-4000-8000-000000000003"),
}
EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=UTC)


def upgrade() -> None:
    """Upgrade schema."""
    model_prices = op.create_table(
        "model_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_microcents_per_mtok", sa.BigInteger(), nullable=False),
        sa.Column("output_microcents_per_mtok", sa.BigInteger(), nullable=False),
        sa.Column("thoughts_microcents_per_mtok", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_prices")),
        sa.UniqueConstraint("model", "effective_from", name=op.f("uq_model_prices_model")),
    )
    op.bulk_insert(
        model_prices,
        [
            {
                "id": PRICE_IDS[model],
                "model": model,
                "effective_from": EFFECTIVE_FROM,
                "input_microcents_per_mtok": microcents(inp),
                "output_microcents_per_mtok": microcents(out),
                "thoughts_microcents_per_mtok": microcents(out),
            }
            for model, (inp, out) in USD_PER_MTOK.items()
        ],
    )
    op.add_column("usage_records", sa.Column("cost_microcents", sa.BigInteger(), nullable=True))
    op.add_column("usage_records", sa.Column("price_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_usage_records_price_id_model_prices"),
        "usage_records",
        "model_prices",
        ["price_id"],
        ["id"],
    )


def microcents(usd: str) -> int:
    """'0.30' dollars -> 30_000_000 micro-cents. Parsed as a string, never a float."""
    dollars, _, cents = usd.partition(".")
    return (int(dollars) * 100 + int(cents.ljust(2, "0")[:2] or 0)) * 1_000_000


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("fk_usage_records_price_id_model_prices"), "usage_records", type_="foreignkey"
    )
    op.drop_column("usage_records", "price_id")
    op.drop_column("usage_records", "cost_microcents")
    op.drop_table("model_prices")
