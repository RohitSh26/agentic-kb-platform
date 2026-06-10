"""Add source identity constraint and single-active build run invariant.

The build engine's idempotent upsert needs (source_type, source_uri) as the natural
identity of a source_item, and architecture invariant 5 requires at most one active
kb_build_run at a time (enforced with a partial unique index on status = 'active').

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_source_item_source_type_source_uri",
        "source_item",
        ["source_type", "source_uri"],
    )
    op.create_index(
        "uq_kb_build_run_single_active",
        "kb_build_run",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_kb_build_run_single_active", table_name="kb_build_run")
    op.drop_constraint("uq_source_item_source_type_source_uri", "source_item", type_="unique")
