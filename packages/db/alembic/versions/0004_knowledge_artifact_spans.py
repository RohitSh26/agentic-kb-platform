"""Add knowledge_artifact.span_start / span_end line-span columns.

PR-06 (graphify adapter): code artifacts record a 1-based inclusive line
span so L2 evidence can return precise snippets at a source version. The
file path comes via source_id -> source_item.path, so only the span lives
here. Nullable on purpose — non-code artifacts (summaries, concepts, wiki
chunks) have no line span.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_artifact", sa.Column("span_start", sa.Integer(), nullable=True))
    op.add_column("knowledge_artifact", sa.Column("span_end", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_artifact", "span_end")
    op.drop_column("knowledge_artifact", "span_start")
