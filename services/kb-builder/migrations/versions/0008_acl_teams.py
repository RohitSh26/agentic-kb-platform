"""Add acl_teams to source_item and knowledge_artifact — team-based ACL metadata.

PR-13 (security hardening): empty array = org-public (any authenticated subject
may read); non-empty = visible only to requesters whose team set intersects.
Enforcement happens in the mcp-server Context Broker (PR-13); kb-builder
connectors populate it in a follow-up, so the empty default preserves current
behavior. NOT NULL with a server default is safe for the same reason as 0007:
existing rows get the default, no backfill needed.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_item",
        sa.Column(
            "acl_teams",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "knowledge_artifact",
        sa.Column(
            "acl_teams",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_artifact", "acl_teams")
    op.drop_column("source_item", "acl_teams")
