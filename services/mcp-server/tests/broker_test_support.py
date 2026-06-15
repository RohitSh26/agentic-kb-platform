"""Seed helpers for broker integration tests against the kb-builder-owned schema.

mcp-server never runs migrations, so these helpers insert via raw SQL into an
externally migrated TEST_DATABASE_URL (kb-builder `make migrate-test-db`).
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.context_broker.budgets import BudgetPolicy
from agentic_mcp_server.context_broker.dependencies import BrokerDeps, BrokerSettings
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient

KB_VERSION = "kb-test"

_REGISTRY_TABLES = (
    "retrieval_event",
    "knowledge_edge",
    "knowledge_artifact",
    "source_item",
    "kb_build_run",
)


async def require_registry_schema(session: AsyncSession) -> None:
    table = await session.execute(text("SELECT to_regclass('retrieval_event')"))
    if table.scalar_one_or_none() is None:
        pytest.skip(
            "registry tables missing — run kb-builder migrations first (make migrate-test-db)"
        )


async def clean_registry(session: AsyncSession) -> None:
    for table in _REGISTRY_TABLES:
        await session.execute(text(f"DELETE FROM {table}"))
    await session.commit()


async def insert_build_run(
    session: AsyncSession, kb_version: str, status: str, *, build_seq: int = 1
) -> None:
    # build_seq is the interval-membership cutoff (version-membership.md): the
    # broker resolves the ACTIVE build's build_seq and serves rows whose
    # valid_from_seq <= it. Default 1 so the default-seeded artifacts/edges
    # (valid_from_seq = 0) are members.
    await session.execute(
        text(
            "INSERT INTO kb_build_run (kb_version, build_seq, status)"
            " VALUES (:kb_version, :build_seq, :status)"
        ),
        {"kb_version": kb_version, "build_seq": build_seq, "status": status},
    )
    await session.commit()


async def insert_artifact(
    session: AsyncSession,
    *,
    kb_version: str = KB_VERSION,
    title: str,
    body_text: str,
    knowledge_kind: str = "source_backed",
    authority_score: float = 0.8,
    artifact_type: str = "doc_chunk",
    source_uri: str | None = None,
    acl_teams: list[str] | None = None,
    source_is_deleted: bool = False,
    valid_from_seq: int = 0,
    invalidated_at_seq: int | None = None,
) -> uuid.UUID:
    source_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO source_item (source_id, source_type, source_uri, source_version,"
            " content_hash, is_deleted) VALUES (CAST(:source_id AS uuid), 'github_doc',"
            " :source_uri, 'rev-1', :content_hash, :is_deleted)"
        ),
        {
            "source_id": str(source_id),
            "source_uri": source_uri or f"https://example.test/{artifact_id}",
            "content_hash": f"hash-{artifact_id}",
            "is_deleted": source_is_deleted,
        },
    )
    await session.execute(
        text(
            "INSERT INTO knowledge_artifact (artifact_id, artifact_type, source_id, title,"
            " body_text, kb_version, knowledge_kind, authority_score, acl_teams,"
            " valid_from_seq, invalidated_at_seq) VALUES"
            " (CAST(:artifact_id AS uuid), :artifact_type, CAST(:source_id AS uuid), :title,"
            " :body_text, :kb_version, :knowledge_kind, :authority_score,"
            " CAST(:acl_teams AS text[]), :valid_from_seq, :invalidated_at_seq)"
        ),
        {
            "artifact_id": str(artifact_id),
            "artifact_type": artifact_type,
            "source_id": str(source_id),
            "title": title,
            "body_text": body_text,
            "kb_version": kb_version,
            "knowledge_kind": knowledge_kind,
            "authority_score": authority_score,
            "acl_teams": acl_teams or [],
            "valid_from_seq": valid_from_seq,
            "invalidated_at_seq": invalidated_at_seq,
        },
    )
    await session.commit()
    return artifact_id


async def insert_edge(
    session: AsyncSession,
    *,
    from_artifact_id: uuid.UUID,
    to_artifact_id: uuid.UUID,
    edge_type: str,
    kb_version: str = KB_VERSION,
    confidence: float = 0.9,
    source: str = "graphify",
    trust_class: str = "EXTRACTED",
    valid_from_seq: int = 0,
    invalidated_at_seq: int | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO knowledge_edge (from_artifact_id, to_artifact_id, edge_type,"
            " confidence, source, kb_version, trust_class, valid_from_seq, invalidated_at_seq)"
            " VALUES (CAST(:from_id AS uuid), CAST(:to_id AS uuid), :edge_type, :confidence,"
            " :source, :kb_version, :trust_class, :valid_from_seq, :invalidated_at_seq)"
        ),
        {
            "from_id": str(from_artifact_id),
            "to_id": str(to_artifact_id),
            "edge_type": edge_type,
            "confidence": confidence,
            "source": source,
            "kb_version": kb_version,
            "trust_class": trust_class,
            "valid_from_seq": valid_from_seq,
            "invalidated_at_seq": invalidated_at_seq,
        },
    )
    await session.commit()


async def fetch_ledger_rows(session: AsyncSession, run_id: str) -> list[Row[Any]]:
    result = await session.execute(
        text(
            "SELECT tool_name, status, cache_hit, semantic_reuse, tokens_returned, agent_name,"
            " reused_evidence_ids, new_evidence_ids FROM retrieval_event"
            " WHERE run_id = :run_id ORDER BY created_at, retrieval_id"
        ),
        {"run_id": run_id},
    )
    return list(result)


def make_broker_deps(
    session_factory: async_sessionmaker[AsyncSession],
    search_client: FakeSearchClient,
    *,
    settings: BrokerSettings | None = None,
    budget_policy: BudgetPolicy | None = None,
) -> BrokerDeps:
    return BrokerDeps(
        session_factory=session_factory,
        search_client=search_client,
        settings=settings or BrokerSettings(),
        budget_policy=budget_policy or BudgetPolicy(),
    )
