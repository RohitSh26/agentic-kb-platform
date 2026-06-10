"""Add generation_cache_artifact table and knowledge_artifact.knowledge_kind.

generation_cache_artifact maps one generation_cache row to its N output
artifacts, in order. cache_key cascades with its cache row; artifact_id has
NO ON DELETE action on purpose — deleting a knowledge_artifact still
referenced by a cached output set must fail (RESTRICT-like default) rather
than silently shrink the set.

knowledge_kind distinguishes "interpreted" knowledge (summaries/concepts)
from "source_backed" evidence so interpreted knowledge can rank below
source-backed evidence (architecture §5).

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_cache_artifact",
        sa.Column(
            "cache_key",
            sa.Text(),
            sa.ForeignKey(
                "generation_cache.cache_key",
                ondelete="CASCADE",
                name="fk_generation_cache_artifact_cache_key",
            ),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "knowledge_artifact.artifact_id",
                name="fk_generation_cache_artifact_artifact_id",
            ),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("cache_key", "artifact_id"),
        sa.UniqueConstraint(
            "cache_key", "position", name="uq_generation_cache_artifact_cache_key_position"
        ),
    )
    # Backfill: pre-0003 cache rows recorded a single output artifact in
    # generation_cache.output_artifact_id. The hit path now reads ONLY the
    # mapping table, so without this backfill those rows would hit the cache
    # but return zero artifacts — silently dropping them from embed/index.
    op.execute(
        "INSERT INTO generation_cache_artifact (cache_key, artifact_id, position) "
        "SELECT cache_key, output_artifact_id, 0 FROM generation_cache "
        "WHERE output_artifact_id IS NOT NULL"
    )
    op.add_column("knowledge_artifact", sa.Column("knowledge_kind", sa.Text(), nullable=True))
    # op.f() keeps the metadata naming convention (ck_%(table_name)s_...) from
    # double-prefixing the already-final constraint name.
    op.create_check_constraint(
        op.f("ck_knowledge_artifact_knowledge_kind"),
        "knowledge_artifact",
        "knowledge_kind IS NULL OR knowledge_kind IN ('interpreted', 'source_backed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_knowledge_artifact_knowledge_kind"), "knowledge_artifact", type_="check"
    )
    op.drop_column("knowledge_artifact", "knowledge_kind")
    op.drop_table("generation_cache_artifact")
