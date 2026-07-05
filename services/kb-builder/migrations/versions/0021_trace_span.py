"""trace_span — per-step tracing for the mcp-server-owned LangGraph graph (ADR-0032).

mcp-server writes one row per span it observes behind its `TraceSink` port: one root span per
`get_task_context` call plus one span per node (`resolve_scope`, `blast_radius`, `conventions`,
`similar_prior_changes`, `synthesize`, and `broaden` when the retry fires), and one root span per
`kb_search` call. Fail-soft lives entirely on the writer side (docs/contracts/tracing.md) — this
table exists purely to receive whatever the writer successfully sends; a dead/slow connection
just means fewer rows, never a failed tool call.

`span_id` is supplied by the application (`uuid4()` at span-creation time, the `Span` DTO's own
field) — no server-side default, unlike the other UUID-PK tables in this registry. Pure derived
observability: never evidence, never read by any retrieval path, safe to prune by age or drop
entirely (same posture as the ADR-0027 model-output cache tables — see migration 0018).

Indexes: `(trace_id)` for "every span in one call", `(name, started_at)` for "slowest spans this
week" — no more (YAGNI; this is not a general-purpose APM store).

Backfill: none — new table. Downgrade drops it. Verified up -> down -> up.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bare constraint token; the metadata naming_convention ("ck_%(table_name)s_%(constraint_name)s",
# see migration 0009) expands the persisted name to ck_trace_span_status.
_STATUS_CHECK_TOKEN = "status"
_STATUSES = ("ok", "error")
_TRACE_ID_INDEX = "ix_trace_span_trace_id"
_NAME_STARTED_AT_INDEX = "ix_trace_span_name_started_at"


def upgrade() -> None:
    op.create_table(
        "trace_span",
        sa.Column("span_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("parent_span_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    bucket_list = ", ".join(f"'{status}'" for status in _STATUSES)
    op.create_check_constraint(
        _STATUS_CHECK_TOKEN,
        "trace_span",
        f"status IN ({bucket_list})",
    )
    op.create_index(_TRACE_ID_INDEX, "trace_span", ["trace_id"])
    op.create_index(_NAME_STARTED_AT_INDEX, "trace_span", ["name", "started_at"])


def downgrade() -> None:
    op.drop_index(_NAME_STARTED_AT_INDEX, table_name="trace_span")
    op.drop_index(_TRACE_ID_INDEX, table_name="trace_span")
    op.drop_constraint(_STATUS_CHECK_TOKEN, "trace_span", type_="check")
    op.drop_table("trace_span")
