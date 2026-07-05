"""Postgres implementation of the TraceSink port (ADR-0032), in the `review_panel` schema.

Bootstrap is idempotent (`CREATE TABLE IF NOT EXISTS`), same pattern as draft_store.py /
checkpointer.py; the connection's search_path is pinned to `review_panel`, so this table can
never collide with — or reach — the Knowledge Registry kb-builder owns.
"""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from review_panel.infrastructure.postgres import REVIEW_PANEL_SCHEMA, review_panel_connection
from review_panel.infrastructure.trace_sink import Span
from review_panel.structured_logging import get_logger

logger = get_logger("review_panel.infrastructure.postgres_trace_sink")

TRACE_SPAN_TABLE = "trace_span"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TRACE_SPAN_TABLE} (
    span_id        uuid PRIMARY KEY,
    trace_id       text NOT NULL,
    parent_span_id uuid,
    name           text NOT NULL,
    service        text NOT NULL,
    started_at     timestamptz NOT NULL,
    ended_at       timestamptz NOT NULL,
    status         text NOT NULL CHECK (status IN ('ok', 'error')),
    attributes     jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
)
"""

_INDEX_TRACE_ID = (
    f"CREATE INDEX IF NOT EXISTS ix_{TRACE_SPAN_TABLE}_trace_id ON {TRACE_SPAN_TABLE} (trace_id)"
)
_INDEX_NAME_STARTED_AT = (
    f"CREATE INDEX IF NOT EXISTS ix_{TRACE_SPAN_TABLE}_name_started_at"
    f" ON {TRACE_SPAN_TABLE} (name, started_at)"
)


class PostgresTraceSink:
    def __init__(self, conn: AsyncConnection[DictRow]) -> None:
        self._conn = conn

    async def emit(self, span: Span) -> None:
        await self._conn.execute(
            f"INSERT INTO {TRACE_SPAN_TABLE} "
            "(span_id, trace_id, parent_span_id, name, service, started_at, ended_at,"
            " status, attributes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                span.span_id,
                span.trace_id,
                span.parent_span_id,
                span.name,
                span.service,
                span.started_at,
                span.ended_at,
                span.status,
                json.dumps(span.attributes) if span.attributes else None,
            ),
        )


@asynccontextmanager
async def postgres_trace_sink(database_url: str) -> AsyncGenerator[PostgresTraceSink, None]:
    """Open a PostgresTraceSink whose table lives only in `review_panel`."""
    async with review_panel_connection(database_url) as conn:
        await conn.execute(_DDL)
        await conn.execute(_INDEX_TRACE_ID)
        await conn.execute(_INDEX_NAME_STARTED_AT)
        logger.info(
            "event=trace_sink_ready schema=%s table=%s", REVIEW_PANEL_SCHEMA, TRACE_SPAN_TABLE
        )
        yield PostgresTraceSink(conn)


__all__ = ["TRACE_SPAN_TABLE", "PostgresTraceSink", "postgres_trace_sink"]
