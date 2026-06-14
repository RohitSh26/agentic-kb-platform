"""Record publish-gate outcomes on kb_build_run (PR-25).

The publish gates (docs/contracts/publish-gates.md) gate kb_version activation:
a failed, non-overridden gate leaves the new version inactive and records WHICH
gate failed and the measured value, so a failed publish is a first-class,
queryable outcome — never a silent skip. Three new columns:

  failed_gate          the gate that blocked activation (NULL on success)
  gate_measured_value  the measured number for that gate (NULL on success)
  allow_large_delta    the symbol-count-delta override flag for this build

extractor_failures backs the extractor-error-rate gate (files that failed AST
extraction during the build). All columns default so existing rows backfill
without an UPDATE pass. Downgrade is a clean reversal.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kb_build_run",
        sa.Column(
            "extractor_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "kb_build_run",
        sa.Column(
            "allow_large_delta",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("kb_build_run", sa.Column("failed_gate", sa.Text(), nullable=True))
    op.add_column("kb_build_run", sa.Column("gate_measured_value", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_build_run", "gate_measured_value")
    op.drop_column("kb_build_run", "failed_gate")
    op.drop_column("kb_build_run", "allow_large_delta")
    op.drop_column("kb_build_run", "extractor_failures")
