"""Draft persistence behind the DraftStore port (docs/contracts/review-panel.md).

One row per draft_key (<repo>#<pr>@<head_sha>). Writes are idempotent by
construction — INSERT ... ON CONFLICT DO NOTHING; the first writer wins and a
racing run reuses the stored row. Tables live only in the `review_panel`
schema (search_path pinned); bootstrap is idempotent.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Protocol

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Json

from review_panel.domain.draft import ReviewDraft
from review_panel.infrastructure.postgres import REVIEW_PANEL_SCHEMA, review_panel_connection
from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.infrastructure.draft_store")

DRAFT_TABLE = "review_draft"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {DRAFT_TABLE} (
    draft_key  text PRIMARY KEY,
    repo       text NOT NULL,
    pr_number  integer NOT NULL,
    head_sha   text NOT NULL,
    draft      jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""


class DraftStore(Protocol):
    """The two draft capabilities the engine needs — nothing else exists."""

    async def get(self, draft_key: str) -> ReviewDraft | None: ...

    async def put_if_absent(self, draft: ReviewDraft) -> ReviewDraft: ...


class InMemoryDraftStore:
    """No-database fallback: single-process durability only (the CLI logs this plainly)."""

    def __init__(self) -> None:
        self._drafts: dict[str, ReviewDraft] = {}

    async def get(self, draft_key: str) -> ReviewDraft | None:
        return self._drafts.get(draft_key)

    async def put_if_absent(self, draft: ReviewDraft) -> ReviewDraft:
        winner = self._drafts.setdefault(draft.draft_key, draft)
        if winner is not draft:
            logger.info("event=draft_store_conflict draft_key=%s reused=true", draft.draft_key)
        return winner


class PostgresDraftStore:
    def __init__(self, conn: AsyncConnection[DictRow]) -> None:
        self._conn = conn

    async def get(self, draft_key: str) -> ReviewDraft | None:
        cursor = await self._conn.execute(
            f"SELECT draft FROM {DRAFT_TABLE} WHERE draft_key = %s", (draft_key,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ReviewDraft.model_validate(row["draft"])

    async def put_if_absent(self, draft: ReviewDraft) -> ReviewDraft:
        cursor = await self._conn.execute(
            f"INSERT INTO {DRAFT_TABLE} (draft_key, repo, pr_number, head_sha, draft) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (draft_key) DO NOTHING",
            (
                draft.draft_key,
                draft.repo,
                draft.pr_number,
                draft.head_sha,
                Json(draft.model_dump(mode="json")),
            ),
        )
        if cursor.rowcount == 0:
            logger.info("event=draft_store_conflict draft_key=%s reused=true", draft.draft_key)
        stored = await self.get(draft.draft_key)
        if stored is None:  # unreachable barring concurrent deletes; fail loud, not silent
            raise RuntimeError(f"draft vanished after write: {draft.draft_key}")
        return stored


@asynccontextmanager
async def postgres_draft_store(database_url: str) -> AsyncGenerator[PostgresDraftStore, None]:
    """Open a PostgresDraftStore whose table lives only in `review_panel`."""
    async with review_panel_connection(database_url) as conn:
        await conn.execute(_DDL)
        logger.info("event=draft_store_ready schema=%s table=%s", REVIEW_PANEL_SCHEMA, DRAFT_TABLE)
        yield PostgresDraftStore(conn)
