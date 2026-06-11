"""Registry seeding for eval cases.

Raw-SQL seed helpers against an externally migrated TEST_DATABASE_URL —
deliberately duplicated from mcp-server's test support per ADR-0008 ("duplicate
small things"; another project's tests/ cannot be imported). Evals never runs
migrations: kb-builder owns the schema (make migrate-test-db).
"""

import uuid

from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from harness.cases import Fixtures

KB_VERSION = "kb-eval"

_REGISTRY_TABLES = (
    "retrieval_event",
    "knowledge_edge",
    "knowledge_artifact",
    "source_item",
    "kb_build_run",
)


class RegistryNotMigratedError(RuntimeError):
    pass


async def require_registry_schema(session: AsyncSession) -> None:
    table = await session.execute(text("SELECT to_regclass('retrieval_event')"))
    if table.scalar_one_or_none() is None:
        raise RegistryNotMigratedError(
            "registry tables missing — run kb-builder migrations first (make migrate-test-db)"
        )


async def clean_registry(session: AsyncSession) -> None:
    for table in _REGISTRY_TABLES:
        await session.execute(text(f"DELETE FROM {table}"))
    await session.commit()


async def seed_case_fixtures(
    session: AsyncSession, fixtures: Fixtures, search: FakeSearchClient
) -> dict[str, uuid.UUID]:
    """Insert the case's artifacts and seed the fake search; returns key -> artifact_id."""
    await session.execute(
        text("INSERT INTO kb_build_run (kb_version, status) VALUES (:kb_version, 'active')"),
        {"kb_version": KB_VERSION},
    )
    ids: dict[str, uuid.UUID] = {}
    for artifact in fixtures.artifacts:
        source_id = uuid.uuid4()
        artifact_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO source_item (source_id, source_type, source_uri, source_version,"
                " content_hash) VALUES (CAST(:source_id AS uuid), 'github_doc', :source_uri,"
                " 'rev-1', :content_hash)"
            ),
            {
                "source_id": str(source_id),
                "source_uri": f"https://example.test/{artifact.key}",
                "content_hash": f"hash-{artifact_id}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_artifact (artifact_id, artifact_type, source_id, title,"
                " body_text, kb_version, knowledge_kind, authority_score) VALUES"
                " (CAST(:artifact_id AS uuid), :artifact_type, CAST(:source_id AS uuid), :title,"
                " :body_text, :kb_version, :knowledge_kind, :authority_score)"
            ),
            {
                "artifact_id": str(artifact_id),
                "artifact_type": artifact.artifact_type,
                "source_id": str(source_id),
                "title": artifact.title,
                "body_text": artifact.body_text,
                "kb_version": KB_VERSION,
                "knowledge_kind": artifact.knowledge_kind,
                "authority_score": artifact.authority_score,
            },
        )
        ids[artifact.key] = artifact_id
    await session.commit()

    for seed in fixtures.search_seeds:
        hits = [
            SearchHit(artifact_id=ids[key], score=float(len(seed.hits) - position))
            for position, key in enumerate(seed.hits)
        ]
        search.seed(seed.keyword, hits)
    return ids
