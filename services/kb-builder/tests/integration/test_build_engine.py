"""Incremental build engine tests (PR-04 acceptance criteria).

DB-backed tests are skipped gracefully when TEST_DATABASE_URL is not
configured, same policy as the registry round-trip tests. Wikify/Graphify/
Embed/Index are spies so the tests prove the gating behavior, not pipeline
output.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application import (
    BuildRunner,
    EmbeddingResult,
    GenerationCacheGate,
    activate_kb_version,
    code_graph_cache_key,
    doc_extract_cache_key,
    get_active_kb_version,
)
from agentic_kb_builder.connectors import GitHubCodeConnector, GitHubDocConnector
from agentic_kb_builder.docify.extractor import DocExtractor
from agentic_kb_builder.domain import (
    DocArtifactDraft,
    DocExtractionResult,
    NormalizedContent,
    SourceRef,
)
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import (
    EmbeddingCache,
    GenerationCache,
    GenerationCacheArtifact,
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
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
)


def test_cache_keys_are_deterministic_and_distinct() -> None:
    key_a = doc_extract_cache_key(
        source_content_hash="h1",
        doc_extract_prompt_version="1.0.0",
        model_name="gpt-test",
        model_params_hash="p1",
        output_schema_version="1.0.0",
    )
    key_b = doc_extract_cache_key(
        source_content_hash="h2",
        doc_extract_prompt_version="1.0.0",
        model_name="gpt-test",
        model_params_hash="p1",
        output_schema_version="1.0.0",
    )
    assert key_a != key_b

    graph_key = code_graph_cache_key(
        repo="o/r",
        commit_sha="sha",
        file_path="a.py",
        file_content_hash="h1",
        graphify_version="1.0.0",
        parser_config_version="1.0.0",
    )
    assert graph_key not in {key_a, key_b}


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
        # clean before AND after: the DB is shared (evals/mcp-server runs leave rows)
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
                    title=f"summary of {content.source.path}",
                    body_text=f"Summary of {content.source.source_uri}",
                    authority_score=0.5,
                    freshness_score=1.0,
                ),
            )
        )


class FailingDocExtractor(SpyDocExtractor):
    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        self.calls += 1
        raise RuntimeError("model exploded")


class SpyEmbedder:
    embedding_model = "embed-test"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(embedding_hash="emb-" + content_hash(text)[:12], vector=[0.5, 0.25])


class SpyIndexer:
    def __init__(self) -> None:
        self.calls = 0
        self.received: list[uuid.UUID] = []
        self.delete_orphaned_calls = 0

    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        self.calls += 1
        self.received.extend(artifact_ids)
        return len(artifact_ids)

    async def delete_orphaned(self) -> int:
        self.delete_orphaned_calls += 1
        return 0

    async def reconcile_missing(self) -> int:
        return 0


URI = "https://github.com/o/r/blob/sha1/a.py"
REF = SourceRef(
    source_type="github_code",
    source_uri=URI,
    source_version="sha1",
    repo="o/r",
    path="a.py",
)

# A prose (github_doc) source for the WIKIFY-pipeline tests. ADR-0018 routes
# github_code to graphify-only (no LLM), so wikify behaviour is now exercised
# through a doc source — the LLM is reserved for prose.
DOC_URI = "https://github.com/o/r/blob/sha1/docs/guide.md"
DOC_REF = SourceRef(
    source_type="github_doc",
    source_uri=DOC_URI,
    source_version="sha1",
    repo="o/r",
    path="docs/guide.md",
)

Spies = tuple[BuildRunner, SpyDocExtractor, SpyEmbedder, SpyIndexer]

# A one-function code file. Whole-tree Graphify yields a code_file artifact plus a
# code_symbol (`get_user`, span 1-2, body_text=the def line) and one `defined_in` edge.
CODE_FN = "def get_user():\n    return 1\n"


def _connector(raw: str) -> GitHubCodeConnector:
    return GitHubCodeConnector(FakeBackend([REF], {URI: raw}))


def _doc_connector(raw: str) -> GitHubDocConnector:
    """A github_doc connector for docify-pipeline tests (graphify never runs on it)."""
    return GitHubDocConnector(FakeBackend([DOC_REF], {DOC_URI: raw}))


def _runner(session: AsyncSession, kb_version: str = "v-test.1") -> Spies:
    doc_extractor = SpyDocExtractor()
    embedder = SpyEmbedder()
    indexer = SpyIndexer()
    runner = BuildRunner(
        session,
        kb_version=kb_version,
        doc_extractor=doc_extractor,
        embedder=embedder,
        indexer=indexer,
    )
    return runner, doc_extractor, embedder, indexer


async def _count(session: AsyncSession, model: type[KnowledgeArtifact] | type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@requires_db
async def test_first_build_processes_then_unchanged_build_skips_everything(
    session: AsyncSession,
) -> None:
    # A github_doc source (ADR-0018): wikify runs, graphify never does.
    runner, doc_extractor, embedder, indexer = _runner(session)
    run1 = await runner.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run1.sources_seen == 1
    assert run1.sources_changed == 1
    assert run1.llm_calls == 1
    # only the summary body is embedded (no code source ⇒ no graphify artifacts)
    assert run1.embedding_calls == 1
    assert (doc_extractor.calls, embedder.calls, indexer.calls) == (1, 1, 1)

    seen_before = (await session.execute(select(SourceItem.last_seen_at))).scalar_one()
    assert seen_before is not None

    runner2, doc_extractor2, embedder2, indexer2 = _runner(session, "v-test.2")
    run2 = await runner2.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run2.sources_seen == 1
    assert run2.sources_changed == 0
    assert run2.llm_calls == 0
    assert run2.embedding_calls == 0
    # unchanged content_hash => chunk/wikify/graphify/embed/index all skipped
    assert (doc_extractor2.calls, embedder2.calls, indexer2.calls) == (0, 0, 0)
    # but the skip path still refreshes last_seen_at for deletion sweeps
    seen_after = (await session.execute(select(SourceItem.last_seen_at))).scalar_one()
    assert seen_after is not None and seen_after > seen_before


@requires_db
async def test_embedding_vector_persisted_and_orphan_sweep_runs(session: AsyncSession) -> None:
    """The vector lands in embedding_cache (index rebuildable without
    re-embedding) and every successful run reconciles index orphans."""
    # a function file ⇒ a code_symbol with body_text that gets embedded.
    runner, *_, indexer = _runner(session)
    await runner.run([_connector(CODE_FN)])
    await session.commit()

    rows = (await session.execute(select(EmbeddingCache))).scalars().all()
    assert rows and all(row.embedding == [0.5, 0.25] for row in rows)
    assert indexer.delete_orphaned_calls == 1


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
    generation/embedding caches already hold this content. No model is called.

    A github_doc source (ADR-0018) exercises the wikify generation-cache gate."""
    runner, *_ = _runner(session)
    await runner.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()

    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()

    runner2, doc_extractor2, embedder2, indexer2 = _runner(session, "v-test.2")
    run2 = await runner2.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run2.sources_changed == 1
    assert run2.llm_calls == 0
    assert run2.embedding_calls == 0
    assert (doc_extractor2.calls, embedder2.calls) == (0, 0)
    assert indexer2.calls == 1  # reindexing reused artifacts is allowed
    # just the wikify summary — no duplicates on retry; doc sources have no edges
    assert await _count(session, KnowledgeArtifact) == 1
    assert await _count(session, KnowledgeEdge) == 0


@requires_db
async def test_reupsert_heals_stale_identity_columns(session: AsyncSession) -> None:
    """Task #30: a source_item row's repo/branch/path/external_id were only ever
    SET at insert time — an on_conflict_do_update that skipped them left a row
    inserted before a connector started stamping them (e.g. repo=NULL) stale
    forever, even once the connector caught up. A genuine re-upsert (content
    actually changed, so the ON CONFLICT branch runs) now heals every identity
    column to the connector's CURRENT SourceRef. The healing is source_item-only:
    the docify artifact this source produces is untouched (the generation-cache
    hit on unchanged real content replays its SAME artifact id, no duplicate).
    The alias miner (PR-38) separately reacts to the renamed path by minting a
    new `alias_reference` phrase artifact/edge — expected, orthogonal behavior
    (covered by its own tests), not a symptom of this fix, so this test does not
    assert on it."""
    uri = "https://github.com/o/r/blob/sha1/docs/stale.md"
    ref_before = SourceRef(
        source_type="github_doc",
        source_uri=uri,
        source_version="sha1",
        repo=None,
        branch=None,
        path="docs/old-name.md",
        external_id=None,
    )
    runner, *_ = _runner(session)
    await runner.run(
        [GitHubDocConnector(FakeBackend([ref_before], {uri: "Some prose to summarize.\n"}))]
    )
    await session.commit()

    # Select individual COLUMNS (not the mapped SourceItem entity): with
    # expire_on_commit=False an entity-level select would return the SAME cached
    # identity-mapped object on a later re-select, masking the very update this
    # test proves (the established idiom elsewhere in this file, e.g.
    # test_first_build_processes_then_unchanged_build_skips_everything).
    source_id_before, repo_before, branch_before, external_id_before = (
        await session.execute(
            select(
                SourceItem.source_id, SourceItem.repo, SourceItem.branch, SourceItem.external_id
            ).where(SourceItem.source_uri == uri)
        )
    ).one()
    assert (repo_before, branch_before, external_id_before) == (None, None, None)
    summary_ids_before = set(
        (
            await session.execute(
                select(KnowledgeArtifact.artifact_id).where(
                    KnowledgeArtifact.source_id == source_id_before,
                    KnowledgeArtifact.artifact_type == "summary",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(summary_ids_before) == 1

    # Force a genuine re-upsert (mirrors the retry idiom above): the stored
    # content_hash no longer matches the real content, so _is_unchanged is False
    # and _upsert_source_item's ON CONFLICT branch runs again — exactly as it
    # would for a source whose content genuinely changed. The connector NOW
    # stamps repo/branch/path/external_id, as it would after e.g. 08d5492.
    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()
    ref_after = ref_before.model_copy(
        update={"repo": "o/r", "branch": "main", "path": "docs/new-name.md", "external_id": "42"}
    )
    runner2, *_ = _runner(session, "v-test.2")
    await runner2.run(
        [GitHubDocConnector(FakeBackend([ref_after], {uri: "Some prose to summarize.\n"}))]
    )
    await session.commit()

    source_id_after, repo_after, branch_after, path_after, external_id_after = (
        await session.execute(
            select(
                SourceItem.source_id,
                SourceItem.repo,
                SourceItem.branch,
                SourceItem.path,
                SourceItem.external_id,
            ).where(SourceItem.source_uri == uri)
        )
    ).one()
    assert source_id_after == source_id_before
    assert repo_after == "o/r"
    assert branch_after == "main"
    assert path_after == "docs/new-name.md"
    assert external_id_after == "42"

    # The healing is identity-columns-only: the docify generation-cache hit (same
    # real content) replays the SAME summary artifact id — no duplicate/rewritten row.
    summary_ids_after = set(
        (
            await session.execute(
                select(KnowledgeArtifact.artifact_id).where(
                    KnowledgeArtifact.source_id == source_id_after,
                    KnowledgeArtifact.artifact_type == "summary",
                )
            )
        )
        .scalars()
        .all()
    )
    assert summary_ids_after == summary_ids_before


@requires_db
async def test_one_source_failure_is_skipped_not_fatal_and_others_persist(
    session: AsyncSession,
) -> None:
    """A single source's extraction failure (e.g. an LLM timeout) is skipped, not fatal: the
    build COMPLETES, the failure is counted, the failed doc leaves nothing behind (and is not
    advanced, so it retries next build), and every source that SUCCEEDED is persisted."""
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        doc_extractor=FailingDocExtractor(),  # every doc extraction raises
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    # one code source (graphify, zero-LLM — succeeds) + one doc source (docify — fails).
    run = await runner.run([_connector(CODE_FN), _doc_connector("Some prose to summarize.\n")])
    await session.commit()

    # the build COMPLETED despite the doc failure — no exception aborted it.
    assert run.status == "completed"
    assert run.extractor_failures == 1
    # the failed doc left no generation-cache row, and its source_item was rolled back (a
    # brand-new source), so it is retried on the next build.
    assert await _count(session, GenerationCache) == 0
    doc_sources = (
        (await session.execute(select(SourceItem).where(SourceItem.source_type == "github_doc")))
        .scalars()
        .all()
    )
    assert doc_sources == []
    # but the code source that SUCCEEDED is persisted.
    code = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.artifact_type.in_(("code_file", "code_symbol"))
                )
            )
        )
        .scalars()
        .all()
    )
    assert code


class _FailOnSecondDocExtractor(SpyDocExtractor):
    """Succeeds on the first document, raises on the second — to prove the first is already
    committed (persisted) by the time the second fails."""

    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        self.calls += 1
        if self.calls >= 2:
            raise RuntimeError("model timeout on the second doc")
        return DocExtractionResult(
            artifacts=(
                DocArtifactDraft(
                    artifact_type="summary",
                    knowledge_kind="interpreted",
                    title=f"summary of {content.source.path}",
                    body_text=f"Summary of {content.source.source_uri}",
                    authority_score=0.5,
                    freshness_score=1.0,
                ),
            )
        )


@requires_db
async def test_earlier_source_is_persisted_when_a_later_one_fails(session: AsyncSession) -> None:
    """Incremental persistence: knowledge is committed AS each source completes, so when a later
    source fails the earlier one is already in the database — not discarded."""
    uri_a = "https://github.com/o/r/blob/sha1/docs/a.md"
    uri_b = "https://github.com/o/r/blob/sha1/docs/b.md"
    refs = [
        SourceRef(
            source_type="github_doc",
            source_uri=uri_a,
            source_version="sha1",
            repo="o/r",
            path="docs/a.md",
        ),
        SourceRef(
            source_type="github_doc",
            source_uri=uri_b,
            source_version="sha1",
            repo="o/r",
            path="docs/b.md",
        ),
    ]
    texts = {uri_a: "First doc.\n", uri_b: "Second doc.\n"}
    connector = GitHubDocConnector(FakeBackend(refs, texts))
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        doc_extractor=_FailOnSecondDocExtractor(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    run = await runner.run([connector])
    await session.commit()

    assert run.status == "completed"
    assert run.extractor_failures == 1
    # the FIRST doc is persisted (committed before the second was even attempted)...
    persisted = (
        (
            await session.execute(
                select(SourceItem.source_uri).where(SourceItem.is_deleted.is_(False))
            )
        )
        .scalars()
        .all()
    )
    assert uri_a in persisted
    # ...and the SECOND (failed) doc left nothing and is retried next build.
    assert uri_b not in persisted
    assert await _count(session, KnowledgeArtifact) >= 1  # the first doc's summary survived


class MultiDraftDocExtractor(SpyDocExtractor):
    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        self.calls += 1
        return DocExtractionResult(
            artifacts=(
                DocArtifactDraft(
                    artifact_type="concept",
                    knowledge_kind="interpreted",
                    title="a",
                    body_text="a",
                    authority_score=0.6,
                    freshness_score=1.0,
                ),
                DocArtifactDraft(
                    artifact_type="concept",
                    knowledge_kind="interpreted",
                    title="b",
                    body_text="b",
                    authority_score=0.6,
                    freshness_score=1.0,
                ),
            )
        )


@requires_db
async def test_multi_artifact_docify_cache_hit_returns_all_artifacts(
    session: AsyncSession,
) -> None:
    """One cache row -> N artifacts via generation_cache_artifact; a hit must
    surface the full set to embed/index (resolves the PR-04 open question)."""
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        doc_extractor=MultiDraftDocExtractor(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    # 2 doc concepts (a github_doc source ⇒ no graphify artifacts)
    assert await _count(session, KnowledgeArtifact) == 2
    # one docify cache row (no graphify cache row for a doc source)
    assert await _count(session, GenerationCache) == 1
    assert await _count(session, GenerationCacheArtifact) == 2
    # the docify mapping preserves generation order (ORDER BY position)
    mapped_titles = (
        (
            await session.execute(
                select(KnowledgeArtifact.title)
                .join(
                    GenerationCacheArtifact,
                    GenerationCacheArtifact.artifact_id == KnowledgeArtifact.artifact_id,
                )
                .where(KnowledgeArtifact.artifact_type == "concept")
                .order_by(GenerationCacheArtifact.position)
            )
        )
        .scalars()
        .all()
    )
    assert mapped_titles == ["a", "b"]

    # retry with a stale-looking source: cache hit, zero model calls, both
    # artifacts still reach embed/index.
    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()
    doc_extractor2 = MultiDraftDocExtractor()
    embedder2 = SpyEmbedder()
    indexer2 = SpyIndexer()
    runner2 = BuildRunner(
        session,
        kb_version="v-test.2",
        doc_extractor=doc_extractor2,
        embedder=embedder2,
        indexer=indexer2,
    )
    run2 = await runner2.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run2.llm_calls == 0
    assert doc_extractor2.calls == 0
    assert embedder2.calls == 0  # embedding cache also hits for every artifact
    assert indexer2.calls == 1
    assert await _count(session, KnowledgeArtifact) == 2
    assert await _count(session, GenerationCacheArtifact) == 2


@requires_db
async def test_generation_cache_record_is_idempotent_for_same_ids(session: AsyncSession) -> None:
    source = SourceItem(
        source_type="github_doc", source_uri="u", source_version="v", content_hash="h"
    )
    session.add(source)
    await session.flush()
    artifact = KnowledgeArtifact(
        artifact_type="summary",
        source_id=source.source_id,
        body_text="t",
        kb_version="v-test.1",
        knowledge_kind="interpreted",
    )
    session.add(artifact)
    await session.flush()

    gate = GenerationCacheGate(session)
    for _ in range(2):  # a build retry re-records the same output set
        await gate.record(
            cache_key="k",
            input_hash="h",
            prompt_version="1.0.0",
            model_name="gpt-test",
            model_params_hash="p",
            output_schema_version="1.0.0",
            output_artifact_ids=[artifact.artifact_id],
        )
    assert await _count(session, GenerationCache) == 1
    assert await _count(session, GenerationCacheArtifact) == 1
    assert await gate.lookup_artifact_ids("k") == [artifact.artifact_id]


DOC_TEXT = (
    "The login flow validates a session token against the AuthMiddleware.\n"
    "Tokens are refreshed by the rotation job.\n"
)


def _captured_extraction(doc_path: str) -> dict[str, Any]:
    """A captured-shape Graphify doc extraction over DOC_TEXT. The grounded concept's
    source_location is a verbatim source sentence; the paraphrased one is not; the
    document node carries a heading. The raw extraction still includes a concept->concept
    relation, which docify deliberately does NOT materialize as an edge (relation-ontology)."""
    return {
        "nodes": [
            {
                "id": "doc",
                "label": "Login guide",
                "file_type": "document",
                "source_file": doc_path,
                "source_location": "# Login guide",
            },
            {
                "id": "login_flow",
                "label": "login flow",
                "file_type": "concept",
                "source_file": doc_path,
                "source_location": (
                    "The login flow validates a session token against the AuthMiddleware."
                ),
            },
            {
                "id": "rotation",
                "label": "token rotation",
                "file_type": "concept",
                "source_file": doc_path,
                "source_location": "The credentials are rotated on a schedule.",
            },
        ],
        "edges": [
            {
                "source": "login_flow",
                "target": "rotation",
                "relation": "conceptually_related_to",
                "confidence": "EXTRACTED",
            }
        ],
        "input_tokens": 10,
        "output_tokens": 5,
    }


def _fake_doc_extractor() -> tuple["DocExtractor", list[int]]:
    """A real DocExtractor wired to an INJECTED fake extract_fn (no live LLM). The list it
    returns counts calls so the cache-gate behaviour is provable."""
    calls = [0]

    async def fake_extract_fn(*, text: str, doc_path: str) -> dict[str, Any]:
        calls[0] += 1
        return _captured_extraction(doc_path)

    extractor = DocExtractor(
        fake_extract_fn,
        model_name="gpt-test",
        model_params_hash="params-test",
    )
    return extractor, calls


@requires_db
async def test_docify_pipeline_cache_miss_writes_then_cache_hit_skips_model(
    session: AsyncSession,
) -> None:
    extractor, calls = _fake_doc_extractor()
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        doc_extractor=extractor,
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    run1 = await runner.run([_doc_connector(DOC_TEXT)])
    await session.commit()
    assert calls[0] == 1
    assert run1.llm_calls == 1

    artifacts = (await session.execute(select(KnowledgeArtifact))).scalars().all()
    by_type = {artifact.artifact_type: artifact for artifact in artifacts}
    # a github_doc source (ADR-0023) ⇒ docify: an interpreted summary (the document node),
    # one source_backed_fact (the verbatim-anchored concept), and one interpreted concept
    # (the paraphrased one). No chunk artifacts — docify replaces wikify's chunker.
    assert sorted(by_type) == ["concept", "source_backed_fact", "summary"]
    summary = by_type["summary"]
    assert summary.knowledge_kind == "interpreted"
    assert summary.authority_score is not None and summary.authority_score < 1.0
    assert summary.freshness_score == 1.0
    backed = by_type["source_backed_fact"]
    assert backed.knowledge_kind == "source_backed"
    # the source_backed body carries the verbatim supporting sentence (L0-confirmable).
    assert backed.body_text is not None and backed.body_text in DOC_TEXT
    assert by_type["concept"].knowledge_kind == "interpreted"
    # docify produces ARTIFACTS ONLY — concept->concept relations are NOT materialized as
    # edges (relation-ontology); the doc path writes no knowledge_edge rows.
    assert await _count(session, KnowledgeEdge) == 0

    # cache hit: stale-looking source, same content => zero model calls, same artifact ids
    prior_ids = {a.artifact_id for a in artifacts}
    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()
    extractor2, calls2 = _fake_doc_extractor()
    runner2 = BuildRunner(
        session,
        kb_version="v-test.2",
        doc_extractor=extractor2,
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    run2 = await runner2.run([_doc_connector(DOC_TEXT)])
    await session.commit()
    assert calls2[0] == 0
    assert run2.llm_calls == 0
    # idempotent: no duplicate artifacts (and no edges), and the SAME artifact ids replayed.
    assert await _count(session, KnowledgeArtifact) == 3
    assert await _count(session, KnowledgeEdge) == 0
    after_ids = {
        a.artifact_id for a in (await session.execute(select(KnowledgeArtifact))).scalars().all()
    }
    assert after_ids == prior_ids


def _multi_connector(refs_to_text: dict[tuple[str, str, str], str]) -> GitHubCodeConnector:
    """Build a github_code connector over several (repo, path, version) -> source files.

    Whole-tree Graphify runs once per repo over all the supplied files, so this lets a
    test feed REAL multi-file Python and assert on real cross-file extraction output.
    """
    refs: list[SourceRef] = []
    texts: dict[str, str] = {}
    for (repo, path, version), source in refs_to_text.items():
        uri = f"https://github.com/{repo}/blob/{version}/{path}"
        refs.append(
            SourceRef(
                source_type="github_code",
                source_uri=uri,
                source_version=version,
                repo=repo,
                path=path,
            )
        )
        texts[uri] = source
    return GitHubCodeConnector(FakeBackend(refs, texts))


@requires_db
async def test_graphify_real_tree_creates_artifacts_and_edges(session: AsyncSession) -> None:
    """Whole-tree Graphify (ADR-0012) over REAL Python: a multi-file repo becomes
    code_file + code_symbol artifacts with exact spans/body_text, plus the deterministic
    edges — `defined_in` (symbol->file), `imports` (file->file), `calls` (symbol->symbol).
    A third file imports stdlib `os`, which is out-of-tree and produces NO file->file edge
    (a dropped reference, never a fabricated node)."""
    a2 = "from b import thing\n\ndef run():\n    return thing()\n"
    b = "def thing():\n    return 2\n"
    c = "import os\n\ndef f():\n    return os.getcwd()\n"
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        doc_extractor=SpyDocExtractor(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run(
        [
            _multi_connector(
                {
                    ("o/r", "a2.py", "sha1"): a2,
                    ("o/r", "b.py", "sha1"): b,
                    ("o/r", "c.py", "sha1"): c,
                }
            )
        ]
    )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(KnowledgeArtifact.kb_version == "v-test.1")
            )
        )
        .scalars()
        .all()
    )
    artifacts = {(row.artifact_type, row.title): row for row in rows}
    # one pointer-only code_file per source file (body_text=None).
    for path in ("a2.py", "b.py", "c.py"):
        assert ("code_file", path) in artifacts
        assert artifacts[("code_file", path)].body_text is None

    run_sym = artifacts[("code_symbol", "run()")]
    assert (run_sym.span_start, run_sym.span_end) == (3, 4)
    assert run_sym.body_text == "def run():\n    return thing()"
    assert run_sym.knowledge_kind == "source_backed"
    thing_sym = artifacts[("code_symbol", "thing()")]
    assert (thing_sym.span_start, thing_sym.span_end) == (1, 2)
    assert thing_sym.body_text == "def thing():\n    return 2"

    a2_file = artifacts[("code_file", "a2.py")]
    b_file = artifacts[("code_file", "b.py")]
    c_file = artifacts[("code_file", "c.py")]

    edges = (await session.execute(select(KnowledgeEdge))).scalars().all()
    assert all(edge.source == "graphify" and edge.kb_version == "v-test.1" for edge in edges)
    edge_set = {(e.edge_type, e.from_artifact_id, e.to_artifact_id) for e in edges}

    # defined_in: every symbol -> its own file (exact AST fact, confidence 1.0).
    assert ("defined_in", run_sym.artifact_id, a2_file.artifact_id) in edge_set
    assert ("defined_in", thing_sym.artifact_id, b_file.artifact_id) in edge_set
    # imports: a2.py -> b.py (resolved within the tree); stdlib `os` never fabricates one.
    imports = [e for e in edges if e.edge_type == "imports"]
    assert {(e.from_artifact_id, e.to_artifact_id) for e in imports} == {
        (a2_file.artifact_id, b_file.artifact_id)
    }
    assert all(e.confidence == 1.0 for e in imports)
    assert c_file.artifact_id not in {e.from_artifact_id for e in imports}
    assert c_file.artifact_id not in {e.to_artifact_id for e in imports}
    # calls: run -> thing (cross-file symbol resolution, the Graphify capability).
    calls = [e for e in edges if e.edge_type == "calls"]
    assert (run_sym.artifact_id, thing_sym.artifact_id) in {
        (e.from_artifact_id, e.to_artifact_id) for e in calls
    }


@requires_db
async def test_cross_file_edges_resolve_within_one_build_regardless_of_order(
    session: AsyncSession,
) -> None:
    """a2.py imports b.py and both change in the same build. The `imports` edge must
    bind to b.py's THIS-build artifact — not a stale one carried from a prior build."""
    b_uri = "https://github.com/o/r/blob/sha1/b.py"
    old_b_source = SourceItem(
        source_type="github_code",
        source_uri=b_uri,
        source_version="sha0",
        repo="o/r",
        path="b.py",
        content_hash="old-b",
    )
    session.add(old_b_source)
    await session.flush()
    old_b_file = KnowledgeArtifact(
        artifact_type="code_file",
        source_id=old_b_source.source_id,
        title="b.py",
        kb_version="v-test.0",
        knowledge_kind="source_backed",
    )
    session.add(old_b_file)
    await session.commit()

    # a2.py imports thing from b.py; both are REAL files changing in this build.
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        doc_extractor=SpyDocExtractor(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run(
        [
            _multi_connector(
                {
                    ("o/r", "a2.py", "sha1"): "from b import thing\n\ndef run():\n"
                    "    return thing()\n",
                    ("o/r", "b.py", "sha1"): "def thing():\n    return 2\n",
                }
            )
        ]
    )
    await session.commit()

    new_b_file_id = (
        await session.execute(
            select(KnowledgeArtifact.artifact_id).where(
                KnowledgeArtifact.artifact_type == "code_file",
                KnowledgeArtifact.title == "b.py",
                KnowledgeArtifact.kb_version == "v-test.1",
            )
        )
    ).scalar_one()
    edge = (
        await session.execute(select(KnowledgeEdge).where(KnowledgeEdge.edge_type == "imports"))
    ).scalar_one()
    assert edge.to_artifact_id == new_b_file_id
    assert edge.to_artifact_id != old_b_file.artifact_id


@requires_db
async def test_same_path_in_two_repos_does_not_cross_bind_edges(session: AsyncSession) -> None:
    """Graphify runs once PER REPO, so two repos sharing a path must not cross-bind:
    o/z's main.py imports its OWN src/utils.py even though o/r's src/utils.py exists."""
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        doc_extractor=SpyDocExtractor(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run(
        [
            _multi_connector(
                {
                    # o/r processed first; its utils.py shares a path with o/z's.
                    ("o/r", "src/utils.py", "sha1"): "Z = 0\n",
                    ("o/z", "main.py", "sha1"): "from src.utils import Z\n\ndef run():\n"
                    "    return Z\n",
                    ("o/z", "src/utils.py", "sha1"): "Z = 1\n",
                }
            )
        ]
    )
    await session.commit()

    z_utils_id = (
        await session.execute(
            select(KnowledgeArtifact.artifact_id)
            .join(SourceItem, KnowledgeArtifact.source_id == SourceItem.source_id)
            .where(
                KnowledgeArtifact.artifact_type == "code_file",
                SourceItem.repo == "o/z",
                SourceItem.path == "src/utils.py",
            )
        )
    ).scalar_one()
    edge = (
        await session.execute(select(KnowledgeEdge).where(KnowledgeEdge.edge_type == "imports"))
    ).scalar_one()
    assert edge.to_artifact_id == z_utils_id


@requires_db
async def test_code_rerun_feeds_this_builds_artifacts_to_index_without_dup_edges(
    session: AsyncSession,
) -> None:
    """Whole-tree code extraction has no per-file graphify generation cache (ADR-0012):
    a code source that looks changed is RE-EXTRACTED, producing this build's own code
    artifacts. Those ids must reach embed/index (or code vanishes from Search), and the
    served edge set must not duplicate — the prior generation is superseded, not stacked."""
    # a one-function file ⇒ exactly 2 code artifacts (code_file + code_symbol).
    runner, *_ = _runner(session)
    await runner.run([_connector(CODE_FN)])
    await session.commit()
    assert (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .where(KnowledgeArtifact.artifact_type.in_(["code_file", "code_symbol"]))
        )
    ).scalar_one() == 2

    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()
    runner2, *_, indexer2 = _runner(session, "v-test.2")
    await runner2.run([_connector(CODE_FN)])
    await session.commit()

    # the re-extraction wrote THIS build's code artifacts; they must reach embed/index.
    new_code_ids = set(
        (
            await session.execute(
                select(KnowledgeArtifact.artifact_id).where(
                    KnowledgeArtifact.artifact_type.in_(["code_file", "code_symbol"]),
                    KnowledgeArtifact.kb_version == "v-test.2",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(new_code_ids) == 2
    assert new_code_ids <= set(indexer2.received)
    # the served graph for v-test.2 carries exactly one defined_in edge — not duplicated.
    served_edges = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(KnowledgeEdge.kb_version == "v-test.2")
        )
    ).scalar_one()
    assert served_edges == 1


@requires_db
async def test_validation_gates_activation(session: AsyncSession) -> None:
    run_v1 = KbBuildRun(kb_version="v1", build_seq=1, status="completed")
    run_v2 = KbBuildRun(kb_version="v2", build_seq=2, status="completed")
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

    run_v3 = KbBuildRun(kb_version="v3", build_seq=3, status="completed")
    session.add(run_v3)
    await session.flush()
    assert await activate_kb_version(session, run_v3.build_id, passes) is True
    await session.commit()
    assert await get_active_kb_version(session) == "v3"
    run_v1_after = await session.get(KbBuildRun, run_v1.build_id)
    assert run_v1_after is not None and run_v1_after.status == "superseded"
