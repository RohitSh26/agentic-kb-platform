"""Postgres implementation of the TraceSink protocol (ADR-0032).

Raw SQL with pinned names (no shared ORM, same idiom as retrieval_events.py) — the column set is
the contract in docs/contracts/tracing.md. One INSERT per span; mcp-server never reads this table
(operator/dashboard concern only, same posture as retrieval_event).
"""

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.infrastructure.tracing.trace_sink import Span

TRACE_SPAN_TABLE = "trace_span"

_INSERT_SPAN_QUERY = text(
    f"""
    INSERT INTO {TRACE_SPAN_TABLE} (
        span_id, trace_id, parent_span_id, name, service,
        started_at, ended_at, status, attributes
    ) VALUES (
        CAST(:span_id AS uuid), :trace_id, CAST(:parent_span_id AS uuid), :name, :service,
        :started_at, :ended_at, :status, CAST(:attributes AS jsonb)
    )
    """
)


async def insert_span(session: AsyncSession, span: Span) -> None:
    await session.execute(
        _INSERT_SPAN_QUERY,
        {
            "span_id": str(span.span_id),
            "trace_id": span.trace_id,
            "parent_span_id": str(span.parent_span_id) if span.parent_span_id else None,
            "name": span.name,
            "service": span.service,
            "started_at": span.started_at,
            "ended_at": span.ended_at,
            "status": span.status,
            "attributes": json.dumps(span.attributes) if span.attributes else None,
        },
    )
    await session.commit()


@dataclass(frozen=True)
class PostgresTraceSink:
    """The default `TraceSink` adapter: one committed INSERT per span."""

    session_factory: async_sessionmaker[AsyncSession]

    async def emit(self, span: Span) -> None:
        async with self.session_factory() as session:
            await insert_span(session, span)


__all__ = ["TRACE_SPAN_TABLE", "PostgresTraceSink", "insert_span"]
