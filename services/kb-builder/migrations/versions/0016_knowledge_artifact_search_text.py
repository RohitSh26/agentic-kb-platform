"""Add knowledge_artifact.search_text column (ADR-0018 Phase 2).

Adds a nullable TEXT column that holds the deterministic retrieval surface
for code_symbol artifacts: split-identifier words + docstring + signature
param names + decorator names + called names + imported names.

Populated on the next build for Python symbols (zero-LLM, content-hash-gated
as today). No backfill required — existing rows stay NULL and become searchable
on the next incremental build.

Down = drop column (a clean reversal; no served KB depends on this column,
it is a redundant retrieval surface derived from body_text / AST).

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_artifact",
        sa.Column("search_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_artifact", "search_text")
