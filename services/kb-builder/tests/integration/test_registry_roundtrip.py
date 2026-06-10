"""Round-trip tests for the Knowledge Registry.

Skipped gracefully when no disposable test database is configured via
TEST_DATABASE_URL (or DATABASE_URL). The URL must use the asyncpg driver,
e.g. postgresql+asyncpg://user:pass@localhost:5432/dbname.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_kb_builder.infrastructure.postgres.models import (
    EmbeddingCache,
    GenerationCache,
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
    RetrievalEvent,
    SourceItem,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config() -> Config:
    assert TEST_DATABASE_URL is not None
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    return Config(str(ALEMBIC_INI))


def test_migration_round_trip() -> None:
    """upgrade head -> downgrade -1 -> upgrade head -> downgrade base (acceptance criteria)."""
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.mark.usefixtures("migrated_db")
async def test_model_round_trip_each_table() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        src = SourceItem(
            source_type="github_code",
            source_uri="https://github.com/org/repo/blob/main/a.py",
            source_version="0123abc",
            repo="org/repo",
            branch="main",
            path="a.py",
            content_hash="hash-src",
        )
        session.add(src)
        await session.flush()

        art_a = KnowledgeArtifact(
            artifact_type="chunk_summary",
            source_id=src.source_id,
            title="a.py summary",
            body_text="Summarizes a.py",
            content_hash="hash-src",
            artifact_hash="hash-art-a",
            kb_version="2026-06-10.1",
            authority_score=0.9,
            freshness_score=1.0,
        )
        art_b = KnowledgeArtifact(
            artifact_type="code_symbol",
            source_id=src.source_id,
            title="def main",
            kb_version="2026-06-10.1",
        )
        session.add_all([art_a, art_b])
        await session.flush()

        session.add_all(
            [
                KnowledgeEdge(
                    from_artifact_id=art_a.artifact_id,
                    to_artifact_id=art_b.artifact_id,
                    edge_type="summarizes",
                    confidence=0.8,
                    source="graphify",
                    kb_version="2026-06-10.1",
                ),
                GenerationCache(
                    cache_key="genkey-1",
                    input_hash="hash-src",
                    prompt_version="1.0.0",
                    model_name="gpt-test",
                    model_params_hash="params-1",
                    output_schema_version="1.0.0",
                    output_artifact_id=art_a.artifact_id,
                ),
                EmbeddingCache(
                    artifact_id=art_a.artifact_id,
                    text_hash="hash-text",
                    embedding_model="embed-test",
                    embedding_hash="hash-emb",
                    azure_search_doc_id="doc-1",
                ),
                KbBuildRun(kb_version="2026-06-10.1", status="succeeded"),
                RetrievalEvent(
                    run_id="run-1",
                    agent_name="orchestrator",
                    tool_name="context.create_pack",
                    query_text="how does a.py work",
                    normalized_query="how does a.py work",
                    retrieval_profile="default",
                    kb_version="2026-06-10.1",
                    source_filters={"source_type": ["github_code"]},
                    returned_artifact_ids=[art_a.artifact_id, art_b.artifact_id],
                    reused_evidence_ids=[],
                    new_evidence_ids=[art_a.artifact_id],
                    tokens_returned=512,
                    latency_ms=42,
                ),
            ]
        )
        await session.commit()

    async with factory() as session:
        got_src = await session.scalar(
            select(SourceItem).where(SourceItem.content_hash == "hash-src")
        )
        assert got_src is not None
        assert got_src.is_deleted is False
        assert got_src.created_at is not None

        got_art = await session.scalar(
            select(KnowledgeArtifact).where(KnowledgeArtifact.artifact_hash == "hash-art-a")
        )
        assert got_art is not None
        assert got_art.source_id == got_src.source_id

        got_edge = await session.scalar(
            select(KnowledgeEdge).where(KnowledgeEdge.edge_type == "summarizes")
        )
        assert got_edge is not None
        assert got_edge.from_artifact_id == got_art.artifact_id

        got_gen = await session.get(GenerationCache, "genkey-1")
        assert got_gen is not None
        assert got_gen.output_artifact_id == got_art.artifact_id

        got_emb = await session.get(
            EmbeddingCache, (got_art.artifact_id, "hash-text", "embed-test")
        )
        assert got_emb is not None
        assert got_emb.embedding_hash == "hash-emb"

        got_run = await session.scalar(select(KbBuildRun))
        assert got_run is not None
        assert got_run.status == "succeeded"
        assert got_run.llm_calls == 0  # server default

        got_event = await session.scalar(
            select(RetrievalEvent).where(RetrievalEvent.run_id == "run-1")
        )
        assert got_event is not None
        assert got_event.cache_hit is False  # server default
        assert got_event.returned_artifact_ids is not None
        assert got_art.artifact_id in got_event.returned_artifact_ids
        assert got_event.new_evidence_ids == [got_art.artifact_id]
        assert got_event.source_filters == {"source_type": ["github_code"]}

        count = await session.scalar(select(func.count()).select_from(RetrievalEvent))
        assert count == 1

    await engine.dispose()
