"""knowledge_artifact.centrality_score — graph-centrality ranking prior.

A normalized [0,1] PageRank over the live knowledge graph, recomputed each build and folded into the
broker rank key as a transparent prior. Nullable: NULL/0 means no graph signal and ranks exactly as
before (backward-safe). Same shape/precedent as authority_score / freshness_score.

Backfill: none — populated by the next build's centrality step. Downgrade drops the column (derived
data; nothing served depends on it — a NULL centrality is the pre-. Verified up->down->up.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_artifact", sa.Column("centrality_score", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_artifact", "centrality_score")
