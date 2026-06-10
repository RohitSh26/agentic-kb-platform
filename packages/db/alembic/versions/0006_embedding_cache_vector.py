"""Store the embedding vector in embedding_cache.

PR-08 (search indexer): the Azure AI Search index must be rebuildable from
Postgres without re-embedding (invariants 1 and 4). embedding_hash alone
cannot reproduce a vector, so the vector itself becomes part of the registry.
Nullable: rows written before this revision (or by builds whose embedder does
not return vectors) simply cannot serve a rebuild and will re-embed once.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "embedding_cache",
        sa.Column("embedding", ARRAY(sa.Float()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("embedding_cache", "embedding")
