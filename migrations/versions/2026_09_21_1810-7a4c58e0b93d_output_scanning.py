"""output scanning: what the response was carrying, and what was done about it

Three more nullable columns on `usage_records`, mirroring the input ones. NULL across all
three means nothing scanned this response - detection off for the fleet, or "off" for the
tenant - which is a different statement from "scanned, found nothing".

`output_findings` holds rule ids, comma separated and sorted, and never the text that matched
them: the text is the customer's response, which has no more business in the ledger than a
prompt does. String(400) rather than something tighter, so a response that trips every rule in
detect/scanner.py is recorded whole instead of truncated at an arbitrary count.

`output_action` has three values rather than two. "blocked" is a response the caller never saw;
"truncated" is a stream stopped part way, where the caller has already read whatever had been
relayed before the finding. Conflating those would let a report claim a leak was contained when
what actually happened is that it was noticed.

No index, for the same reason as the input columns: every query over them is a report about one
tenant and one range of time, which the covering index on (tenant_id, created_at) answers.

Revision ID: 7a4c58e0b93d
Revises: 6e2b91d7c4af
Create Date: 2026-09-21 18:10:34.552104

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a4c58e0b93d"
down_revision: str | Sequence[str] | None = "6e2b91d7c4af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("usage_records", sa.Column("output_verdict", sa.String(length=16), nullable=True))
    op.add_column(
        "usage_records", sa.Column("output_findings", sa.String(length=400), nullable=True)
    )
    op.add_column("usage_records", sa.Column("output_action", sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("usage_records", "output_action")
    op.drop_column("usage_records", "output_findings")
    op.drop_column("usage_records", "output_verdict")
