"""Add retrieval_event.details JSONB — per-tool observability payload.

Adds a nullable JSONB column that holds the rich, per-action detail emitted
by each Context Broker tool (create_pack, expand, open_evidence,
get_neighbors, verify_answer) and the governance.checkpoint gate events.

Never blocks a tool: the column is best-effort and nullable.

Down = drop column (clean reversal; existing rows are unaffected and the
column is derived observability, not truth).

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "retrieval_event",
        sa.Column("details", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("retrieval_event", "details")
