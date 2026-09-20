"""per-tenant monthly budget

No server default, unlike the rate limit columns. NULL means unlimited, and that is the
right default here: a rate limit is a technical cap with a sane generic value, while a
budget is what a customer agreed to pay. Inventing a number would cut paying tenants off
part way through a month.

Revision ID: 8ba9f6fcdcf9
Revises: 491a901be7a6
Create Date: 2026-09-20 14:55:09.771244

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ba9f6fcdcf9"
down_revision: str | Sequence[str] | None = "491a901be7a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("tenants", sa.Column("monthly_budget_microcents", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tenants", "monthly_budget_microcents")
