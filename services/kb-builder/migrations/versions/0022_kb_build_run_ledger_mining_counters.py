"""kb_build_run ledger-mining counters (PR-43, ADR-0034).

Three plain build-run counters — `ledger_mining_misses_seen`, `ledger_mining_mined`,
`ledger_mining_unresolved` — populated once per build from that build's
`LedgerMiningResult` (`alias/ledger_mining.py`). Same idiom as the existing
`extractor_failures` / `llm_calls` counters: `INTEGER NOT NULL DEFAULT 0`, no FK, no
index (never queried directly — only rolled up by day in `v_retrieval_health`,
migration `0023`). kb-builder never writes `retrieval_event` (mcp-server's ledger
alone), so these build-owned counters are the safe surface the dashboard's
mined-vs-unresolved split reads instead (`docs/contracts/observability-dashboard.md`
"Mined-vs-unresolved split").

Backfill: none — existing rows default to 0 (no build before this PR ran ledger
mining, so 0 is the true historical value, not a placeholder). Downgrade drops the
three columns. Verified up -> down -> up.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("ledger_mining_misses_seen", "ledger_mining_mined", "ledger_mining_unresolved")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(
            "kb_build_run",
            sa.Column(column, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("kb_build_run", column)
