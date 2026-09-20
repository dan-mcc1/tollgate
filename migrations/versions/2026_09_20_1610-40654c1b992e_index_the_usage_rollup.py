"""index the usage rollup

Every read of usage_records is "one tenant, one range of time": the budget check that
runs before each request, and the spend rollup. Without this index both are a sequential
scan of every row the gateway has ever written, by every tenant.

INCLUDE carries the columns both queries read, so they can be answered from the index
alone. Measured on 400k rows: 7.7 ms sequential, 1.1 ms with a plain (tenant_id,
created_at), 0.2 ms with this one - and 8334, 2508 and 33 buffers respectively.

The caveat, since the number above is a best case: an index-only scan may still visit the
heap for rows whose page is not yet marked all-visible, and on an append-only table that
means the newest rows - exactly the ones a current-month query reads. Expect the live
figure to sit between the last two until autovacuum catches up.

Built CONCURRENTLY, because the table is append-only and busy: a plain CREATE INDEX takes
a lock that blocks inserts for its whole duration, which on a live gateway means blocking
the ledger write at the end of every request.

bench/rollup_plan.py prints all three plans.

Revision ID: 40654c1b992e
Revises: 8ba9f6fcdcf9
Create Date: 2026-09-20 16:10:33.402117

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "40654c1b992e"
down_revision: str | Sequence[str] | None = "8ba9f6fcdcf9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_usage_records_tenant_id_created_at"


def upgrade() -> None:
    """Upgrade schema."""
    # CONCURRENTLY cannot run inside a transaction, and Alembic wraps migrations in one.
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX,
            "usage_records",
            ["tenant_id", "created_at"],
            postgresql_include=[
                "model",
                "input_tokens",
                "output_tokens",
                "thoughts_tokens",
                "cost_microcents",
            ],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX,
            table_name="usage_records",
            postgresql_concurrently=True,
            if_exists=True,
        )
