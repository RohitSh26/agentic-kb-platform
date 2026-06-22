"""Crash-durable model-output cache (ADR-0027 / PR-35).

Proves the property the whole PR exists for: if a build crashes after paying the model
but before its single end-commit, the artifact-coupled generation_cache / embedding_cache
roll back, yet the *model outputs* survive (side-committed), so the re-run makes ZERO model
calls — while the crashed build never activates (atomicity preserved).

DB-backed; skipped gracefully when TEST_DATABASE_URL is not configured (same policy as the
other integration tests). Never run against the :55432 demo DB — use TEST_DATABASE_URL.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application import BuildRunner, EmbeddingResult
from agentic_kb_builder.connectors import GitHubDocConnector
from agentic_kb_builder.domain import (
    DocArtifactDraft,
    DocExtractionResult,
    NormalizedContent,
    SourceRef,
)
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.durable_output_cache import (
    PostgresDurableOutputCache,
)
from agentic_kb_builder.infrastructure.postgres.models import (
    DocExtractionOutput,
    EmbeddingOutput,
    GenerationCache,
    KbBuildRun,
    KnowledgeArtifact,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

TABLES_IN_DELETE_ORDER = (
    "retrieval_event",
    "embedding_cache",
    "generation_cache_artifact",
    "generation_cache",
    "knowledge_edge",
    "knowledge_artifact",
    "source_item",
    "kb_build_run",
    "doc_extraction_output",
    "embedding_output",
)

DOC_URI = "https://github.com/o/r/blob/sha1/docs/guide.md"
DOC_REF = SourceRef(
    source_type="github_doc",
    source_uri=DOC_URI,
    source_version="sha1",
    repo="o/r",
    path="docs/guide.md",
)


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    assert TEST_DATABASE_URL is not None
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous


@pytest.fixture
async def session(migrated_db: None) -> AsyncIterator[AsyncSession]:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        for table in TABLES_IN_DELETE_ORDER:
            await sess.execute(text(f"DELETE FROM {table}"))
        await sess.commit()
        yield sess
        await sess.rollback()
        for table in TABLES_IN_DELETE_ORDER:
            await sess.execute(text(f"DELETE FROM {table}"))
        await sess.commit()
    await engine.dispose()


class FakeBackend:
    def __init__(self, sources: list[SourceRef], texts: dict[str, str]) -> None:
        self._sources = sources
        self._texts = texts

    async def list_sources(self) -> list[SourceRef]:
        return self._sources

    async def fetch_text(self, source: SourceRef) -> str:
        return self._texts[source.source_uri]


class SpyDocExtractor:
    model_name = "gpt-test"
    model_params_hash = "params-test"

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        self.calls += 1
        return DocExtractionResult(
            artifacts=(
                DocArtifactDraft(
                    artifact_type="summary",
                    knowledge_kind="interpreted",
                    title="summary",
                    body_text=f"Summary of {content.source.source_uri}",
                    authority_score=0.5,
                    freshness_score=1.0,
                ),
            )
        )


class SpyEmbedder:
    embedding_model = "embed-test"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(embedding_hash="emb-" + content_hash(text)[:12], vector=[0.5, 0.25])


class HealthyIndexer:
    def __init__(self) -> None:
        self.received: list[uuid.UUID] = []

    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        self.received.extend(artifact_ids)
        return len(artifact_ids)

    async def delete_orphaned(self) -> int:
        return 0

    async def reconcile_missing(self) -> int:
        return 0


class CrashingIndexer(HealthyIndexer):
    """Upserts succeed (so docify/embed run + side-commit), then the post-source index
    reconcile raises — crashing the build AFTER the model was paid but BEFORE the commit."""

    async def delete_orphaned(self) -> int:
        raise RuntimeError("simulated crash after sources processed")


def _doc_connector(raw: str) -> GitHubDocConnector:
    return GitHubDocConnector(FakeBackend([DOC_REF], {DOC_URI: raw}))


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@requires_db
async def test_crash_then_rerun_makes_zero_model_calls(session: AsyncSession) -> None:
    assert TEST_DATABASE_URL is not None
    durable = PostgresDurableOutputCache.from_url(TEST_DATABASE_URL)
    try:
        # --- build 1: pays the model, then crashes before the end-commit ---
        extractor1, embedder1 = SpyDocExtractor(), SpyEmbedder()
        runner1 = BuildRunner(
            session,
            kb_version="v-test.1",
            doc_extractor=extractor1,
            embedder=embedder1,
            indexer=CrashingIndexer(),
            durable_cache=durable,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            await runner1.run([_doc_connector("Some prose to summarize.\n")])
        # the model WAS paid this run
        assert extractor1.calls == 1
        assert embedder1.calls == 1

        # atomicity: the build rolled back — no served artifacts, no generation_cache,
        # and no active version (the failed run never activates).
        assert await _count(session, KnowledgeArtifact) == 0
        assert await _count(session, GenerationCache) == 0
        active = (
            await session.execute(
                select(KbBuildRun).where(KbBuildRun.status == "active")
            )
        ).scalars().all()
        assert active == []

        # but the model OUTPUTS survived (side-committed, independent of the rollback)
        assert await _count(session, DocExtractionOutput) == 1
        assert await _count(session, EmbeddingOutput) == 1

        # --- build 2: re-run with fresh spies + the SAME durable cache ---
        extractor2, embedder2 = SpyDocExtractor(), SpyEmbedder()
        runner2 = BuildRunner(
            session,
            kb_version="v-test.2",
            doc_extractor=extractor2,
            embedder=embedder2,
            indexer=HealthyIndexer(),
            durable_cache=durable,
        )
        run2 = await runner2.run([_doc_connector("Some prose to summarize.\n")])
        await session.commit()

        # the whole point: ZERO re-paid model calls, yet the build completes with artifacts
        assert run2.status == "completed"
        assert extractor2.calls == 0
        assert embedder2.calls == 0
        assert run2.llm_calls == 0
        assert run2.embedding_calls == 0
        assert await _count(session, KnowledgeArtifact) >= 1
    finally:
        await durable.aclose()


@requires_db
async def test_durable_put_is_idempotent(session: AsyncSession) -> None:
    assert TEST_DATABASE_URL is not None
    durable = PostgresDurableOutputCache.from_url(TEST_DATABASE_URL)
    try:
        result = DocExtractionResult(
            artifacts=(
                DocArtifactDraft(
                    artifact_type="summary",
                    knowledge_kind="interpreted",
                    title="t",
                    body_text="b",
                    authority_score=0.5,
                    freshness_score=1.0,
                ),
            )
        )
        kwargs = dict(
            cache_key="k1",
            input_hash="h",
            prompt_version="1",
            model_name="m",
            model_params_hash="p",
            output_schema_version="1",
            result=result,
        )
        await durable.put_doc_extraction(**kwargs)  # type: ignore[arg-type]
        await durable.put_doc_extraction(**kwargs)  # type: ignore[arg-type]  # no duplicate / no error
        assert await _count(session, DocExtractionOutput) == 1
        got = await durable.get_doc_extraction("k1")
        assert got == result
        assert await durable.get_doc_extraction("missing") is None

        emb = EmbeddingResult(embedding_hash="e", vector=[0.1, 0.2])
        await durable.put_embedding(text_hash="th", embedding_model="em", result=emb)
        await durable.put_embedding(text_hash="th", embedding_model="em", result=emb)
        assert await _count(session, EmbeddingOutput) == 1
        assert await durable.get_embedding(text_hash="th", embedding_model="em") == emb
    finally:
        await durable.aclose()
