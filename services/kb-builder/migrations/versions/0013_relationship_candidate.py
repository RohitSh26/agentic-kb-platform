"""relationship_candidate audit table.

The cheap, deterministic candidate generator (docs/contracts/relationship-candidates.md)
writes cross-domain artifact PAIRS it thinks are worth the phase-3B LLM judge looking
at. A candidate is NOT an edge: no edge_type, no trust_class, never served through MCP.

Audit / measurement artifact ONLY: this table is NOT part of the served KB, so it
deliberately carries NO membership columns (no valid_from_seq / invalidated_at_seq) —
mcp-server never reads it. kb_version is a logging label only.

Idempotency: a partial unique index on (from_artifact_id, to_artifact_id, kb_version)
backs an upsert so a re-run of the same build re-writes the same candidate in place
rather than accreting duplicate rows.

Backfill: none — new table.

Downgrade drops the table and its indexes — a clean reversal (no data the served KB
depends on; this is a measurement artifact).

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relationship_candidate",
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "from_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_artifact.artifact_id"),
            nullable=False,
        ),
        sa.Column(
            "to_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_artifact.artifact_id"),
            nullable=False,
        ),
        # which signals fired + their scores: {signal_name: score, ...}
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        # coarse audit bucket: high / medium / low (CHECK keeps the vocabulary closed)
        sa.Column("candidate_recall_bucket", sa.Text(), nullable=False),
        # the build label that generated the candidate — logging only, NOT membership.
        sa.Column("kb_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "candidate_recall_bucket IN ('high', 'medium', 'low')",
            name="candidate_recall_bucket",
        ),
    )
    op.create_index(
        "ix_relationship_candidate_from_artifact_id",
        "relationship_candidate",
        ["from_artifact_id"],
    )
    op.create_index(
        "ix_relationship_candidate_to_artifact_id",
        "relationship_candidate",
        ["to_artifact_id"],
    )
    op.create_index(
        "ix_relationship_candidate_kb_version",
        "relationship_candidate",
        ["kb_version"],
    )
    # Idempotency: one candidate per (from, to, kb_version); a re-run of the same
    # build upserts in place instead of accreting duplicates.
    op.create_index(
        "uq_relationship_candidate_pair_version",
        "relationship_candidate",
        ["from_artifact_id", "to_artifact_id", "kb_version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_relationship_candidate_pair_version", table_name="relationship_candidate")
    op.drop_index("ix_relationship_candidate_kb_version", table_name="relationship_candidate")
    op.drop_index("ix_relationship_candidate_to_artifact_id", table_name="relationship_candidate")
    op.drop_index("ix_relationship_candidate_from_artifact_id", table_name="relationship_candidate")
    op.drop_table("relationship_candidate")
