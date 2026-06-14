"""Add relation_schema_version + evidence to knowledge_edge (PR-26, ADR-0010 phase 2).

The deterministic cross-domain linker writes edges that carry (a) the relation
ontology version they were produced under and (b) an evidence pointer — the
deterministic match key, matched reference, or changed-file path
(docs/contracts/relation-ontology.md "Required edge fields"). Both columns land
here:

- relation_schema_version: Integer NOT NULL, server_default '1' so every
  existing graphify/linker edge backfills to version 1 with no UPDATE pass. The
  ontology version is part of the relationship-judgment cache key; pinning it on
  the row lets a later vocabulary bump be detected per-edge.
- evidence: JSONB, nullable. Nullable so the pre-existing graphify/linker edges
  (whose evidence pointer is implicit in source/edge_type) are untouched; the
  cross-domain linker populates it for every edge it writes.

Downgrade drops both columns — a clean reversal (no data migration to undo).

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_edge",
        sa.Column(
            "relation_schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "knowledge_edge",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_edge", "evidence")
    op.drop_column("knowledge_edge", "relation_schema_version")
