"""Add retrieval_event.status — the broker's per-call outcome.

: ledger.list_retrievals reports a status per retrieval
(approved/reused/denied/needs_human_approval) so budget denials and reuse are
auditable. NOT NULL with a server default is safe: the broker is this table's
only writer and ships in the same PR, so the table is empty everywhere.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "retrieval_event",
        sa.Column("status", sa.Text(), nullable=False, server_default="approved"),
    )


def downgrade() -> None:
    op.drop_column("retrieval_event", "status")
