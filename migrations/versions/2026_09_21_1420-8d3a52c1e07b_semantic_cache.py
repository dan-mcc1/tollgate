"""semantic cache

Adds the vector half of the response cache.

`cache_entries.embedding` holds the prompt as the embedding model saw it, and
`cache_entries.params_key` holds a hash of everything about the request *except* the
prompt. Both are needed for a semantic hit to be safe: similarity is measured over the
prompt alone, so the system instruction, tools, temperature, response schema, model and
tenant all have to match exactly before two entries are ever compared. Both are nullable,
because an entry written while the tier was switched off has neither, and an entry with a
NULL embedding is invisible to the vector search rather than wrongly matched by it.

The index is HNSW rather than IVFFlat. IVFFlat partitions the space by clustering a
sample of the rows, so an index built on an empty table sorts everything into one list
and has to be rebuilt once there is data - the wrong shape for a cache that starts empty.
HNSW builds a navigable small-world graph incrementally: correct from the first row, and
still correct as rows arrive. It costs more to build and more memory to hold, which at
the size a response cache reaches is the cheaper of the two mistakes.

`vector_cosine_ops` matches the distance operator the lookup uses. An index built for a
different operator is simply not used by a cosine query, silently and with no error.

`usage_records` gains `cache_similarity`, which is the evidence for a hit that later
turns out to be wrong, and `embedding_cost_microcents`, which is what the tier spent
looking. It is kept apart from `cost_microcents` because it is the gateway's own spend
rather than the tenant's generation, and netting it against the saving is the only honest
way to say whether the tier pays for itself.

Revision ID: 8d3a52c1e07b
Revises: c47e1b2a9f04
Create Date: 2026-09-21 14:20:41.118207

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "8d3a52c1e07b"
down_revision: str | Sequence[str] | None = "c47e1b2a9f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIMENSIONS = 768
INDEX = "ix_cache_entries_embedding"


def upgrade() -> None:
    """Upgrade schema."""
    # Here rather than only in the local Compose init script, so the test database and
    # Neon both get it. Creating it needs rights a plain application role may not have,
    # which is worth knowing at migration time rather than at the first lookup.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("cache_entries", sa.Column("params_key", sa.String(length=64), nullable=True))
    op.add_column("cache_entries", sa.Column("embedding", Vector(DIMENSIONS), nullable=True))
    op.add_column("usage_records", sa.Column("cache_similarity", sa.Float(), nullable=True))
    op.add_column(
        "usage_records", sa.Column("embedding_cost_microcents", sa.BigInteger(), nullable=True)
    )

    # CONCURRENTLY, like the ledger's index: an HNSW build is slow, and a plain
    # CREATE INDEX holds a lock that blocks the inserts a live cache is making.
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX,
            "cache_entries",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX, table_name="cache_entries", postgresql_concurrently=True, if_exists=True
        )
    op.drop_column("usage_records", "embedding_cost_microcents")
    op.drop_column("usage_records", "cache_similarity")
    op.drop_column("cache_entries", "embedding")
    op.drop_column("cache_entries", "params_key")
    # The extension is left in place: other things may depend on it, and dropping it is
    # not the inverse of "create if not exists".
