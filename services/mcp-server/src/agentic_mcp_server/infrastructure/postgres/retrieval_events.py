"""retrieval_event ledger access: the broker's only Postgres writes.

Raw SQL with pinned names (no shared ORM); the column set is the contract in
docs/contracts/postgres-knowledge-registry.md and the contract tests keep the
two in sync. Every broker tool call inserts exactly one row.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RETRIEVAL_EVENT_TABLE = "retrieval_event"
RETRIEVAL_STATUS_COLUMN = "status"

_INSERT_EVENT_QUERY = text(
    f"""
    INSERT INTO {RETRIEVAL_EVENT_TABLE} (
        run_id, context_pack_id, agent_name, tool_name, {RETRIEVAL_STATUS_COLUMN},
        query_text, normalized_query, retrieval_profile, kb_version,
        returned_artifact_ids, reused_evidence_ids, new_evidence_ids,
        cache_hit, semantic_reuse, tokens_returned, latency_ms
    ) VALUES (
        :run_id, CAST(:context_pack_id AS uuid), :agent_name, :tool_name, :status,
        :query_text, :normalized_query, :retrieval_profile, :kb_version,
        CAST(:returned_artifact_ids AS uuid[]), CAST(:reused_evidence_ids AS uuid[]),
        CAST(:new_evidence_ids AS uuid[]),
        :cache_hit, :semantic_reuse, :tokens_returned, :latency_ms
    )
    """
)

_LIST_EVENTS_QUERY = text(
    f"""
    SELECT retrieval_id, run_id, kb_version, agent_name, tool_name,
           {RETRIEVAL_STATUS_COLUMN} AS status, cache_hit, tokens_returned,
           reused_evidence_ids, new_evidence_ids, created_at
    FROM {RETRIEVAL_EVENT_TABLE}
    WHERE run_id = :run_id
    ORDER BY created_at, retrieval_id
    """
)

# Every artifact id this subject has actually had returned to it, across the
# three evidence-bearing columns. The L0 verifier uses this so an agent cannot
# cite evidence it never retrieved (verification-receipt.md "in requester
# ledger"). Attribution is by authenticated session subject = agent_name.
_SUBJECT_RETRIEVED_QUERY = text(
    f"""
    SELECT DISTINCT unnested AS artifact_id
    FROM {RETRIEVAL_EVENT_TABLE},
         LATERAL unnest(
             COALESCE(returned_artifact_ids, '{{}}')
             || COALESCE(reused_evidence_ids, '{{}}')
             || COALESCE(new_evidence_ids, '{{}}')
         ) AS unnested
    WHERE agent_name = :agent_name
    """
)


def _empty_uuid_list() -> list[uuid.UUID]:
    return []


@dataclass(frozen=True)
class RetrievalEventInsert:
    run_id: str
    agent_name: str
    tool_name: str
    status: str
    kb_version: str
    context_pack_id: uuid.UUID | None = None
    query_text: str | None = None
    normalized_query: str | None = None
    retrieval_profile: str | None = None
    returned_artifact_ids: list[uuid.UUID] = field(default_factory=_empty_uuid_list)
    reused_evidence_ids: list[uuid.UUID] = field(default_factory=_empty_uuid_list)
    new_evidence_ids: list[uuid.UUID] = field(default_factory=_empty_uuid_list)
    cache_hit: bool = False
    semantic_reuse: bool = False
    tokens_returned: int = 0
    latency_ms: int | None = None


@dataclass(frozen=True)
class RetrievalEventRow:
    retrieval_id: uuid.UUID
    run_id: str
    kb_version: str
    agent_name: str
    tool_name: str
    status: str
    cache_hit: bool
    tokens_returned: int
    reused_evidence_ids: list[uuid.UUID]
    new_evidence_ids: list[uuid.UUID]
    created_at: datetime


async def insert_event(session: AsyncSession, event: RetrievalEventInsert) -> None:
    await session.execute(
        _INSERT_EVENT_QUERY,
        {
            "run_id": event.run_id,
            "context_pack_id": str(event.context_pack_id) if event.context_pack_id else None,
            "agent_name": event.agent_name,
            "tool_name": event.tool_name,
            "status": event.status,
            "query_text": event.query_text,
            "normalized_query": event.normalized_query,
            "retrieval_profile": event.retrieval_profile,
            "kb_version": event.kb_version,
            "returned_artifact_ids": event.returned_artifact_ids,
            "reused_evidence_ids": event.reused_evidence_ids,
            "new_evidence_ids": event.new_evidence_ids,
            "cache_hit": event.cache_hit,
            "semantic_reuse": event.semantic_reuse,
            "tokens_returned": event.tokens_returned,
            "latency_ms": event.latency_ms,
        },
    )
    await session.commit()


async def fetch_subject_retrieved_ids(session: AsyncSession, agent_name: str) -> set[uuid.UUID]:
    """Every artifact id ever returned to this subject across all runs."""
    result = await session.execute(_SUBJECT_RETRIEVED_QUERY, {"agent_name": agent_name})
    return {row.artifact_id for row in result}


async def list_events(session: AsyncSession, run_id: str) -> list[RetrievalEventRow]:
    result = await session.execute(_LIST_EVENTS_QUERY, {"run_id": run_id})
    return [
        RetrievalEventRow(
            retrieval_id=row.retrieval_id,
            run_id=row.run_id,
            kb_version=row.kb_version,
            agent_name=row.agent_name,
            tool_name=row.tool_name,
            status=row.status,
            cache_hit=row.cache_hit,
            tokens_returned=row.tokens_returned or 0,
            reused_evidence_ids=list(row.reused_evidence_ids or []),
            new_evidence_ids=list(row.new_evidence_ids or []),
            created_at=row.created_at,
        )
        for row in result
    ]
