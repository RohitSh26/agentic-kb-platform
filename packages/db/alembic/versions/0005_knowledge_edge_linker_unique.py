"""Add partial unique index backing linker edge idempotency.

PR-07 (linker): uq_knowledge_edge_linker is the ON CONFLICT target that
lets the linker upsert edges idempotently on re-runs. It is partial
(source = 'linker') so graphify's legitimately repeated edges are
unaffected.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_knowledge_edge_linker",
        "knowledge_edge",
        ["from_artifact_id", "to_artifact_id", "edge_type", "kb_version"],
        unique=True,
        postgresql_where=sa.text("source = 'linker'"),
    )


def downgrade() -> None:
    op.drop_index("uq_knowledge_edge_linker", table_name="knowledge_edge")
