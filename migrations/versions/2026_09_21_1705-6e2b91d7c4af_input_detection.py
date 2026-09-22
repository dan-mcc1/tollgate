"""input detection: per-tenant policy, and the verdict on every row

`tenants.detection_mode` is backfilled to "monitor" by the server default rather than left
NULL, so every tenant that already exists is inspected from the moment this lands. Monitor
and not block: nothing is refused until somebody has read this detector's false positive
rate against real traffic, which is what phase 7's eval harness produces. The check
constraint names the three legal values, because the gateway reads this column on every
request and an unknown value would have to be interpreted.

`usage_records` gains five nullable columns. NULL across all five means nothing inspected
this request - detection off for the fleet, or "off" for the tenant - which is a different
statement from "inspected, found clean", and the ledger keeps the difference the way it
already does for `cache_status`. `input_rule` holds a rule id and never the text that
matched it: the text is the prompt, and the prompt does not go in the ledger.

No index. Every query that reads these columns is a report over one tenant and one range of
time, which the covering index on (tenant_id, created_at) already answers; an index on a
column with three distinct values would be written on every insert to help nothing.

Revision ID: 6e2b91d7c4af
Revises: 8d3a52c1e07b
Create Date: 2026-09-21 17:05:12.884301

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6e2b91d7c4af"
down_revision: str | Sequence[str] | None = "8d3a52c1e07b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_MODE = "monitor"
MODE_CONSTRAINT = "ck_tenants_detection_mode"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tenants",
        sa.Column(
            "detection_mode", sa.String(length=16), nullable=False, server_default=DEFAULT_MODE
        ),
    )
    op.create_check_constraint(
        MODE_CONSTRAINT, "tenants", "detection_mode IN ('off', 'monitor', 'block')"
    )

    op.add_column("usage_records", sa.Column("input_verdict", sa.String(length=16), nullable=True))
    op.add_column("usage_records", sa.Column("input_tier", sa.String(length=16), nullable=True))
    op.add_column("usage_records", sa.Column("input_rule", sa.String(length=64), nullable=True))
    op.add_column("usage_records", sa.Column("input_score", sa.Float(), nullable=True))
    op.add_column("usage_records", sa.Column("input_action", sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("usage_records", "input_action")
    op.drop_column("usage_records", "input_score")
    op.drop_column("usage_records", "input_rule")
    op.drop_column("usage_records", "input_tier")
    op.drop_column("usage_records", "input_verdict")
    op.drop_constraint(MODE_CONSTRAINT, "tenants", type_="check")
    op.drop_column("tenants", "detection_mode")
