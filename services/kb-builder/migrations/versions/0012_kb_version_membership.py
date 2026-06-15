"""kb_version interval membership + identity-over-time (PR-27, ADR-0013).

A KB version becomes an INTERVAL MEMBERSHIP keyed to a monotonic build sequence,
replacing label-equality (docs/contracts/version-membership.md). Three changes:

- kb_build_run.build_seq BIGINT — monotonic, assigned at run start from the
  kb_build_seq Postgres SEQUENCE, UNIQUE. Existing rows are backfilled by
  created_at (started_at) ascending -> 1..N, and the sequence is advanced past N
  so the next build_seq never collides.
- knowledge_artifact / knowledge_edge gain valid_from_seq BIGINT NOT NULL
  DEFAULT 0 (the build that introduced the row) and invalidated_at_seq BIGINT
  NULL (set when the row leaves the KB). A row is a member of version S iff
  valid_from_seq <= S AND (invalidated_at_seq IS NULL OR invalidated_at_seq > S).
- knowledge_artifact.prior_identity_id UUID NULL -> knowledge_artifact(artifact_id):
  the rename link, so history survives a path change.

Backfill: every existing artifact/edge gets valid_from_seq = 0, invalidated_at_seq
= NULL -> member of all versions >= 0. The membership read indexes
(valid_from_seq, invalidated_at_seq) support the predicate without dropping the
existing kb_version indexes.

Downgrade drops all added columns, indexes, the sequence, and the unique
constraint in reverse — a clean reversal (the backfilled interval columns carry
no information the kb_version label did not already imply).

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUILD_SEQ_SEQUENCE = "kb_build_seq"


def upgrade() -> None:
    # --- kb_build_run.build_seq (monotonic, backfilled, unique) ---
    op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {BUILD_SEQ_SEQUENCE} START WITH 1"))
    # nullable first so the backfill can populate it deterministically by age.
    op.add_column("kb_build_run", sa.Column("build_seq", sa.BigInteger(), nullable=True))
    # Backfill existing rows 1..N by start time ascending (oldest build = 1).
    op.execute(
        sa.text(
            """
            UPDATE kb_build_run AS r
            SET build_seq = ranked.rn
            FROM (
                SELECT build_id,
                       ROW_NUMBER() OVER (ORDER BY started_at ASC, build_id ASC) AS rn
                FROM kb_build_run
            ) AS ranked
            WHERE r.build_id = ranked.build_id
            """
        )
    )
    # Advance the sequence past the highest backfilled value so the next nextval()
    # never collides with a backfilled build_seq. With rows present, setval(.., N,
    # true) -> next is N+1. An empty table has no max, so setval(.., 1, false) ->
    # next is 1 (is_called=false). setval with value 0 is out of bounds, so the
    # empty case must use 1/false rather than COALESCE(max,0).
    op.execute(
        sa.text(
            f"""
            SELECT setval(
                '{BUILD_SEQ_SEQUENCE}',
                COALESCE((SELECT MAX(build_seq) FROM kb_build_run), 1),
                (SELECT MAX(build_seq) IS NOT NULL FROM kb_build_run)
            )
            """
        )
    )
    op.alter_column("kb_build_run", "build_seq", nullable=False)
    op.create_unique_constraint("uq_kb_build_run_build_seq", "kb_build_run", ["build_seq"])

    # --- knowledge_artifact validity interval + rename link ---
    op.add_column(
        "knowledge_artifact",
        sa.Column("valid_from_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "knowledge_artifact",
        sa.Column("invalidated_at_seq", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "knowledge_artifact",
        sa.Column(
            "prior_identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_artifact.artifact_id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_knowledge_artifact_membership",
        "knowledge_artifact",
        ["valid_from_seq", "invalidated_at_seq"],
    )

    # --- knowledge_edge validity interval ---
    op.add_column(
        "knowledge_edge",
        sa.Column("valid_from_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "knowledge_edge",
        sa.Column("invalidated_at_seq", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_knowledge_edge_membership",
        "knowledge_edge",
        ["valid_from_seq", "invalidated_at_seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_edge_membership", table_name="knowledge_edge")
    op.drop_column("knowledge_edge", "invalidated_at_seq")
    op.drop_column("knowledge_edge", "valid_from_seq")

    op.drop_index("ix_knowledge_artifact_membership", table_name="knowledge_artifact")
    op.drop_column("knowledge_artifact", "prior_identity_id")
    op.drop_column("knowledge_artifact", "invalidated_at_seq")
    op.drop_column("knowledge_artifact", "valid_from_seq")

    op.drop_constraint("uq_kb_build_run_build_seq", "kb_build_run", type_="unique")
    op.drop_column("kb_build_run", "build_seq")
    op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {BUILD_SEQ_SEQUENCE}"))
