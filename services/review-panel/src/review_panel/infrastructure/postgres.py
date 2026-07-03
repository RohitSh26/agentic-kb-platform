"""Connections pinned to the DEDICATED `review_panel` Postgres schema.

kb-builder is the sole owner of the Knowledge Registry (public schema); every
connection this service opens resolves unqualified names ONLY into
`review_panel` via search_path — there is no fallback entry, so no registry
table is reachable (asserted by tests/integration/test_draft_store_schema.py).
Schema bootstrap is idempotent.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

REVIEW_PANEL_SCHEMA = "review_panel"


def to_psycopg_url(database_url: str) -> str:
    """Accept SQLAlchemy-style URLs (postgresql+asyncpg://...) for operator convenience."""
    scheme, _, rest = database_url.partition("://")
    return f"{scheme.split('+', 1)[0]}://{rest}"


@asynccontextmanager
async def review_panel_connection(
    database_url: str,
) -> AsyncGenerator[AsyncConnection[DictRow], None]:
    """Open an autocommit connection whose search_path is only `review_panel`."""
    conn = await AsyncConnection[DictRow].connect(
        to_psycopg_url(database_url),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={REVIEW_PANEL_SCHEMA}",
    )
    try:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {REVIEW_PANEL_SCHEMA}")
        yield conn
    finally:
        await conn.close()
