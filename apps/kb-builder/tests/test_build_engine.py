"""Incremental build engine tests (PR-04 acceptance criteria).

DB-backed tests are skipped gracefully when TEST_DATABASE_URL is not
configured, same policy as packages/db tests. Wikify/Graphify/Embed/Index are
spies so the tests prove the gating behavior, not pipeline output.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from common.hashing import content_hash
from contracts.artifact_schemas import NormalizedContent, SourceRef
from db.models import GenerationCache, KbBuildRun, KnowledgeArtifact, KnowledgeEdge, SourceItem
from kb_builder.build import (
    ArtifactDraft,
    BuildRunner,
    EdgeDraft,
    activate_kb_version,
    chunk_summary_cache_key,
    code_graph_cache_key,
    concept_rollup_cache_key,
    get_active_kb_version,
)
from kb_builder.connectors import GitHubCodeConnector

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages" / "db" / "alembic.ini"

TABLES_IN_DELETE_ORDER = (
    "retrieval_event",
    "embedding_cache",
    "generation_cache",
    "knowledge_edge",
    "knowledge_artifact",
    "source_item",
    "kb_build_run",
)


def test_cache_keys_are_deterministic_and_distinct() -> None:
    key_a = chunk_summary_cache_key(
        source_content_hash="h1",
        chunker_version="1.0.0",
        wikify_prompt_version="1.0.0",
        model_name="gpt-test",
        model_params_hash="p1",
        output_schema_version="1.0.0",
    )
    key_b = chunk_summary_cache_key(
        source_content_hash="h2",
        chunker_version="1.0.0",
        wikify_prompt_version="1.0.0",
        model_name="gpt-test",
        model_params_hash="p1",
        output_schema_version="1.0.0",
    )
    assert key_a != key_b

    rollup_one = concept_rollup_cache_key(
        concept_id="c1",
        supporting_artifact_hashes=["b", "a"],
        rollup_prompt_version="1.0.0",
        model_name="gpt-test",
        output_schema_version="1.0.0",
    )
    rollup_two = concept_rollup_cache_key(
        concept_id="c1",
        supporting_artifact_hashes=["a", "b"],
        rollup_prompt_version="1.0.0",
        model_name="gpt-test",
        output_schema_version="1.0.0",
    )
    assert rollup_one == rollup_two  # supporting-hash order must not matter

    graph_key = code_graph_cache_key(
        repo="o/r",
        commit_sha="sha",
        file_path="a.py",
        file_content_hash="h1",
        graphify_version="1.0.0",
        parser_config_version="1.0.0",
    )
    assert graph_key not in {key_a, key_b, rollup_one}


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


class SpyWikifier:
    model_name = "gpt-test"
    model_params_hash = "params-test"

    def __init__(self) -> None:
        self.calls = 0

    async def wikify(self, content: NormalizedContent) -> Sequence[ArtifactDraft]:
        self.calls += 1
        return [
            ArtifactDraft(
                artifact_type="chunk_summary",
                title=f"summary of {content.source.path}",
                body_text=f"Summary of {content.source.source_uri}",
            )
        ]


class FailingWikifier(SpyWikifier):
    async def wikify(self, content: NormalizedContent) -> Sequence[ArtifactDraft]:
        self.calls += 1
        raise RuntimeError("model exploded")


class SpyGraphifier:
    def __init__(self) -> None:
        self.calls = 0

    async def graphify(
        self, content: NormalizedContent, artifact_ids: Sequence[uuid.UUID]
    ) -> Sequence[EdgeDraft]:
        self.calls += 1
        if not artifact_ids:
            return []
        return [
            EdgeDraft(
                from_artifact_id=artifact_ids[0],
                to_artifact_id=artifact_ids[0],
                edge_type="defined_in",
                confidence=1.0,
                source="graphify",
            )
        ]


class SpyEmbedder:
    embedding_model = "embed-test"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> str:
        self.calls += 1
        return "emb-" + content_hash(text)[:12]


class SpyIndexer:
    def __init__(self) -> None:
        self.calls = 0

    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        self.calls += 1
        return len(artifact_ids)


URI = "https://github.com/o/r/blob/sha1/a.py"
REF = SourceRef(
    source_type="github_code",
    source_uri=URI,
    source_version="sha1",
    repo="o/r",
    path="a.py",
)

Spies = tuple[BuildRunner, SpyWikifier, SpyGraphifier, SpyEmbedder, SpyIndexer]


def _connector(raw: str) -> GitHubCodeConnector:
    return GitHubCodeConnector(FakeBackend([REF], {URI: raw}))


def _runner(session: AsyncSession, kb_version: str = "v-test.1") -> Spies:
    wikifier = SpyWikifier()
    graphifier = SpyGraphifier()
    embedder = SpyEmbedder()
    indexer = SpyIndexer()
    runner = BuildRunner(
        session,
        kb_version=kb_version,
        wikifier=wikifier,
        graphifier=graphifier,
        embedder=embedder,
        indexer=indexer,
    )
    return runner, wikifier, graphifier, embedder, indexer


async def _count(session: AsyncSession, model: type[KnowledgeArtifact] | type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@requires_db
async def test_first_build_processes_then_unchanged_build_skips_everything(
    session: AsyncSession,
) -> None:
    runner, wikifier, graphifier, embedder, indexer = _runner(session)
    run1 = await runner.run([_connector("x = 1\n")])
    await session.commit()
    assert run1.sources_seen == 1
    assert run1.sources_changed == 1
    assert run1.llm_calls == 1
    assert run1.embedding_calls == 1
    assert (wikifier.calls, graphifier.calls, embedder.calls, indexer.calls) == (1, 1, 1, 1)

    seen_before = (await session.execute(select(SourceItem.last_seen_at))).scalar_one()
    assert seen_before is not None

    runner2, wikifier2, graphifier2, embedder2, indexer2 = _runner(session, "v-test.2")
    run2 = await runner2.run([_connector("x = 1\n")])
    await session.commit()
    assert run2.sources_seen == 1
    assert run2.sources_changed == 0
    assert run2.llm_calls == 0
    assert run2.embedding_calls == 0
    # unchanged content_hash => chunk/wikify/graphify/embed/index all skipped
    assert (wikifier2.calls, graphifier2.calls, embedder2.calls, indexer2.calls) == (0, 0, 0, 0)
    # but the skip path still refreshes last_seen_at for deletion sweeps
    seen_after = (await session.execute(select(SourceItem.last_seen_at))).scalar_one()
    assert seen_after is not None and seen_after > seen_before


@requires_db
async def test_rerun_is_idempotent_no_duplicate_rows(session: AsyncSession) -> None:
    runner, *_ = _runner(session)
    await runner.run([_connector("x = 1\n")])
    await session.commit()
    counts_after_first = (
        await _count(session, SourceItem),
        await _count(session, KnowledgeArtifact),
        await _count(session, KnowledgeEdge),
        await _count(session, GenerationCache),
    )

    runner2, *_ = _runner(session, "v-test.2")
    await runner2.run([_connector("x = 1\n")])
    await session.commit()
    counts_after_second = (
        await _count(session, SourceItem),
        await _count(session, KnowledgeArtifact),
        await _count(session, KnowledgeEdge),
        await _count(session, GenerationCache),
    )
    assert counts_after_first == counts_after_second


@requires_db
async def test_cache_hit_prevents_model_calls_even_when_source_looks_changed(
    session: AsyncSession,
) -> None:
    """Simulates a retry after a partial failure: source_item looks stale but the
    generation/embedding caches already hold this content. No model is called."""
    runner, *_ = _runner(session)
    await runner.run([_connector("x = 1\n")])
    await session.commit()

    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()

    runner2, wikifier2, graphifier2, embedder2, indexer2 = _runner(session, "v-test.2")
    run2 = await runner2.run([_connector("x = 1\n")])
    await session.commit()
    assert run2.sources_changed == 1
    assert run2.llm_calls == 0
    assert run2.embedding_calls == 0
    assert (wikifier2.calls, graphifier2.calls, embedder2.calls) == (0, 0, 0)
    assert indexer2.calls == 1  # reindexing reused artifacts is allowed
    assert await _count(session, KnowledgeArtifact) == 1
    assert await _count(session, KnowledgeEdge) == 1


@requires_db
async def test_failed_wikify_leaves_no_cache_row_or_artifacts_but_audit_row_survives(
    session: AsyncSession,
) -> None:
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=FailingWikifier(),
        graphifier=SpyGraphifier(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    with pytest.raises(RuntimeError, match="model exploded"):
        await runner.run([_connector("x = 1\n")])
    # partial work was rolled back by the runner...
    assert await _count(session, GenerationCache) == 0
    assert await _count(session, KnowledgeArtifact) == 0
    assert await _count(session, SourceItem) == 0
    # ...but the build failure is recorded, never silent
    failed_run = (await session.execute(select(KbBuildRun))).scalar_one()
    assert failed_run.status == "failed"
    assert failed_run.error_summary is not None
    assert "model exploded" in failed_run.error_summary
    assert failed_run.completed_at is not None


class MultiDraftWikifier(SpyWikifier):
    async def wikify(self, content: NormalizedContent) -> Sequence[ArtifactDraft]:
        self.calls += 1
        return [
            ArtifactDraft(artifact_type="chunk_summary", title="a", body_text="a"),
            ArtifactDraft(artifact_type="chunk_summary", title="b", body_text="b"),
        ]


@requires_db
async def test_multi_artifact_wikify_fails_loudly_until_pr05_contract(
    session: AsyncSession,
) -> None:
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=MultiDraftWikifier(),
        graphifier=SpyGraphifier(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    with pytest.raises(NotImplementedError, match="PR-05"):
        await runner.run([_connector("x = 1\n")])
    assert await _count(session, KnowledgeArtifact) == 0
    assert await _count(session, GenerationCache) == 0


@requires_db
async def test_validation_gates_activation(session: AsyncSession) -> None:
    run_v1 = KbBuildRun(kb_version="v1", status="completed")
    run_v2 = KbBuildRun(kb_version="v2", status="completed")
    session.add_all([run_v1, run_v2])
    await session.flush()

    async def passes(_session: AsyncSession, _kb_version: str) -> bool:
        return True

    async def fails(_session: AsyncSession, _kb_version: str) -> bool:
        return False

    assert await activate_kb_version(session, run_v1.build_id, passes) is True
    await session.commit()
    assert await get_active_kb_version(session) == "v1"

    # failed validation never flips the active version
    assert await activate_kb_version(session, run_v2.build_id, fails) is False
    await session.commit()
    assert await get_active_kb_version(session) == "v1"
    run_v2_after = await session.get(KbBuildRun, run_v2.build_id)
    assert run_v2_after is not None and run_v2_after.status == "validation_failed"

    run_v3 = KbBuildRun(kb_version="v3", status="completed")
    session.add(run_v3)
    await session.flush()
    assert await activate_kb_version(session, run_v3.build_id, passes) is True
    await session.commit()
    assert await get_active_kb_version(session) == "v3"
    run_v1_after = await session.get(KbBuildRun, run_v1.build_id)
    assert run_v1_after is not None and run_v1_after.status == "superseded"
