"""per-tenant rate limits

Existing tenants are backfilled by the server default rather than left NULL: NULL means
"unlimited" here, and silently exempting every tenant that already exists is the wrong
way round for a gateway.

Revision ID: 491a901be7a6
Revises: b5bb707f7f7a
Create Date: 2026-09-20 12:10:47.203118

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "491a901be7a6"
down_revision: str | Sequence[str] | None = "b5bb707f7f7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_RPM = "60"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tenants",
        sa.Column("rate_limit_rpm", sa.Integer(), nullable=True, server_default=DEFAULT_RPM),
    )
    op.add_column(
        "tenants",
        sa.Column("rate_limit_burst", sa.Integer(), nullable=True, server_default=DEFAULT_RPM),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tenants", "rate_limit_burst")
    op.drop_column("tenants", "rate_limit_rpm")
