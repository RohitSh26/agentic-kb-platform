"""Cross-schema READ of the review-panel-owned `review_draft` table (PR-41, ADR-0031).

mcp-server owns no `review_panel` schema — `services/review-panel` does, and
remains the sole writer (docs/contracts/review-panel.md). This module reads
that schema from mcp-server's OWN Postgres connection (``DATABASE_URL``,
``context_broker/dependencies.py``), schema-qualified — the same cross-schema
READ posture this server already has on the kb-builder-owned Knowledge
Registry (a reader that does not own the schema it reads). It deliberately
duplicates only the tiny row shape from the Draft table (ADR-0008: no shared
Python packages) — never review-panel's Python code or its full
`ReviewDraft` pydantic model.

Deployments where `DATABASE_URL` and `REVIEW_PANEL_DATABASE_URL` point at
DIFFERENT Postgres databases cannot use this tool (a documented V1 limitation,
review-panel.md "Fetching drafts over MCP" — not solved here): the
`review_panel` schema/table is then simply absent from this connection, and
the query below raises, which the caller treats as an unexpected error, not a
"no draft yet" not-found result.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REVIEW_PANEL_SCHEMA = "review_panel"
REVIEW_DRAFT_TABLE = f"{REVIEW_PANEL_SCHEMA}.review_draft"

# head_sha omitted (NULL) -> newest stored draft for (repo, pr_number); given ->
# the exact match. The CAST pins the bind parameter's type so both occurrences of
# the same named parameter agree (asyncpg positional binding, `session.py`).
_SELECT_DRAFT_QUERY = text(
    f"""
    SELECT draft_key, repo, pr_number, head_sha, draft, created_at
    FROM {REVIEW_DRAFT_TABLE}
    WHERE repo = :repo AND pr_number = :pr_number
      AND (CAST(:head_sha AS text) IS NULL OR head_sha = CAST(:head_sha AS text))
    ORDER BY created_at DESC
    LIMIT 1
    """
)


@dataclass(frozen=True)
class ReviewDraftRow:
    """The tiny row shape (review-panel.md's Draft table), duplicated per ADR-0008."""

    draft_key: str
    repo: str
    pr_number: int
    head_sha: str
    draft: dict[str, Any]
    created_at: datetime


async def fetch_review_draft(
    session: AsyncSession, *, repo: str, pr_number: int, head_sha: str | None
) -> ReviewDraftRow | None:
    """Exact match when ``head_sha`` is given; else the newest draft for the PR."""
    result = await session.execute(
        _SELECT_DRAFT_QUERY, {"repo": repo, "pr_number": pr_number, "head_sha": head_sha}
    )
    row = result.first()
    if row is None:
        return None
    return ReviewDraftRow(
        draft_key=row.draft_key,
        repo=row.repo,
        pr_number=row.pr_number,
        head_sha=row.head_sha,
        draft=row.draft,
        created_at=row.created_at,
    )
