"""Add trust_class to knowledge_edge — the trust-bucket vocabulary.

: every edge carries a trust bucket
from {EXTRACTED, INFERRED_HIGH, INFERRED_LOW, AMBIGUOUS, REJECTED}
(docs/contracts/trust-buckets.md). The deterministic producers (AST extractor,
linker) may only ever assign EXTRACTED, so the server default backfills every
existing row to EXTRACTED with no separate UPDATE pass. The broker
(mcp-server) enforces the bucket at read time via trust_floor; the column ships
now — before any INFERRED edge exists — so traversal already treats
lower-trust edges as routing hints when they arrive (phase 3). The CHECK
constraint pins the closed bucket set; the (kb_version, trust_class) index
supports trust-floored traversal scans. Downgrade is a clean reversal.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRUST_BUCKETS = ("EXTRACTED", "INFERRED_HIGH", "INFERRED_LOW", "AMBIGUOUS", "REJECTED")
# Bare constraint token; the metadata naming_convention ("ck_%(table_name)s_%(constraint_name)s")
# expands the persisted name to ck_knowledge_edge_trust_class.
_CHECK_TOKEN = "trust_class"
_INDEX_NAME = "ix_knowledge_edge_kb_version_trust_class"


def upgrade() -> None:
    bucket_list = ", ".join(f"'{bucket}'" for bucket in _TRUST_BUCKETS)
    op.add_column(
        "knowledge_edge",
        sa.Column(
            "trust_class",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'EXTRACTED'"),
        ),
    )
    op.create_check_constraint(
        _CHECK_TOKEN,
        "knowledge_edge",
        f"trust_class IN ({bucket_list})",
    )
    op.create_index(
        _INDEX_NAME,
        "knowledge_edge",
        ["kb_version", "trust_class"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="knowledge_edge")
    op.drop_constraint(_CHECK_TOKEN, "knowledge_edge", type_="check")
    op.drop_column("knowledge_edge", "trust_class")
