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
from contracts.artifact_schemas import (
    Chunk,
    ConceptDraft,
    FileGraph,
    GraphifyResult,
    NormalizedContent,
    ParsedCall,
    ParsedEndpoint,
    ParsedImport,
    ParsedSymbol,
    ParsedTest,
    SourceBackedFactDraft,
    SourceRef,
    WikifyArtifactDraft,
    WikifyGeneration,
)
from db.models import (
    GenerationCache,
    GenerationCacheArtifact,
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from kb_builder.build import (
    BuildRunner,
    GenerationCacheGate,
    activate_kb_version,
    chunk_summary_cache_key,
    code_graph_cache_key,
    concept_rollup_cache_key,
    get_active_kb_version,
)
from kb_builder.connectors import GitHubCodeConnector
from kb_builder.graphify_adapter import file_graph_to_artifacts, file_graph_to_edges
from kb_builder.wikify import WikifyGenerator

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages" / "db" / "alembic.ini"

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

    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
        self.calls += 1
        return [
            WikifyArtifactDraft(
                artifact_type="summary",
                knowledge_kind="interpreted",
                title=f"summary of {content.source.path}",
                body_text=f"Summary of {content.source.source_uri}",
                authority_score=0.5,
                freshness_score=1.0,
            )
        ]


class FailingWikifier(SpyWikifier):
    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
        self.calls += 1
        raise RuntimeError("model exploded")


class SpyGraphifier:
    """Runs the real adapter over a tiny fixed graph: code_file + one symbol
    spanning line 1, plus one self-referential calls edge."""

    def __init__(self) -> None:
        self.calls = 0

    async def graphify(self, content: NormalizedContent) -> GraphifyResult:
        self.calls += 1
        graph = FileGraph(
            path=content.source.path or "",
            symbols=(ParsedSymbol(name="f", kind="function", span_start=1, span_end=1),),
            calls=(ParsedCall(from_symbol="f", to_symbol="f"),),
        )
        return GraphifyResult(
            artifacts=file_graph_to_artifacts(graph, file_text=content.text),
            edges=file_graph_to_edges(graph),
        )


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
        self.received: list[uuid.UUID] = []

    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        self.calls += 1
        self.received.extend(artifact_ids)
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
    # summary body + code_symbol snippet are embedded; code_file is pointer-only
    assert run1.embedding_calls == 2
    assert (wikifier.calls, graphifier.calls, embedder.calls, indexer.calls) == (1, 1, 2, 1)

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
    # summary + code_file + code_symbol, and the calls edge — no duplicates on retry
    assert await _count(session, KnowledgeArtifact) == 3
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
    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
        self.calls += 1
        return [
            WikifyArtifactDraft(
                artifact_type="concept",
                knowledge_kind="interpreted",
                title="a",
                body_text="a",
                authority_score=0.6,
                freshness_score=1.0,
            ),
            WikifyArtifactDraft(
                artifact_type="concept",
                knowledge_kind="interpreted",
                title="b",
                body_text="b",
                authority_score=0.6,
                freshness_score=1.0,
            ),
        ]


@requires_db
async def test_multi_artifact_wikify_cache_hit_returns_all_artifacts(
    session: AsyncSession,
) -> None:
    """One cache row -> N artifacts via generation_cache_artifact; a hit must
    surface the full set to embed/index (resolves the PR-04 open question)."""
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=MultiDraftWikifier(),
        graphifier=SpyGraphifier(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run([_connector("x = 1\n")])
    await session.commit()
    # 2 wikify concepts + 2 graphify code artifacts (code_file + code_symbol)
    assert await _count(session, KnowledgeArtifact) == 4
    # one wikify cache row + one graphify cache row (github_code source)
    assert await _count(session, GenerationCache) == 2
    assert await _count(session, GenerationCacheArtifact) == 4
    # the wikify mapping preserves generation order (ORDER BY position)
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
    wikifier2 = MultiDraftWikifier()
    embedder2 = SpyEmbedder()
    indexer2 = SpyIndexer()
    runner2 = BuildRunner(
        session,
        kb_version="v-test.2",
        wikifier=wikifier2,
        graphifier=SpyGraphifier(),
        embedder=embedder2,
        indexer=indexer2,
    )
    run2 = await runner2.run([_connector("x = 1\n")])
    await session.commit()
    assert run2.llm_calls == 0
    assert wikifier2.calls == 0
    assert embedder2.calls == 0  # embedding cache also hits for every artifact
    assert indexer2.calls == 1
    assert await _count(session, KnowledgeArtifact) == 4
    assert await _count(session, GenerationCacheArtifact) == 4


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


class FakeModelClient:
    model_name = "gpt-test"
    model_params_hash = "params-test"

    def __init__(self, generation: WikifyGeneration) -> None:
        self.calls = 0
        self._generation = generation

    async def generate_wikify(
        self, *, chunks: Sequence[Chunk], prompt_version: str
    ) -> WikifyGeneration:
        self.calls += 1
        return self._generation


@requires_db
async def test_wikify_pipeline_cache_miss_writes_then_cache_hit_skips_model(
    session: AsyncSession,
) -> None:
    raw = "def f():\n    return 1\n"
    generation = WikifyGeneration(
        summary="Defines f returning 1.",
        concepts=(ConceptDraft(name="f", description="A function returning 1."),),
        facts=(
            SourceBackedFactDraft(statement="f returns 1", quote="return 1"),
            SourceBackedFactDraft(statement="invented", quote="not in the source"),
        ),
    )
    model_client = FakeModelClient(generation)
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=WikifyGenerator(model_client),
        graphifier=SpyGraphifier(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    run1 = await runner.run([_connector(raw)])
    await session.commit()
    assert model_client.calls == 1
    assert run1.llm_calls == 1

    artifacts = (await session.execute(select(KnowledgeArtifact))).scalars().all()
    by_type = {artifact.artifact_type: artifact for artifact in artifacts}
    # wikify: 1 chunk + summary + concept + the quote-backed fact (the invented
    # fact is dropped); graphify: code_file + code_symbol
    assert sorted(by_type) == [
        "chunk",
        "code_file",
        "code_symbol",
        "concept",
        "source_backed_fact",
        "summary",
    ]
    summary = by_type["summary"]
    assert summary.knowledge_kind == "interpreted"
    assert summary.authority_score is not None and summary.authority_score < 1.0
    assert summary.freshness_score == 1.0
    assert by_type["chunk"].knowledge_kind == "source_backed"
    assert by_type["source_backed_fact"].knowledge_kind == "source_backed"
    assert "not in the source" not in {a.body_text for a in artifacts}

    # cache hit: stale-looking source, same content => zero model calls
    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()
    model_client2 = FakeModelClient(generation)
    runner2 = BuildRunner(
        session,
        kb_version="v-test.2",
        wikifier=WikifyGenerator(model_client2),
        graphifier=SpyGraphifier(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    run2 = await runner2.run([_connector(raw)])
    await session.commit()
    assert model_client2.calls == 0
    assert run2.llm_calls == 0
    assert await _count(session, KnowledgeArtifact) == 6


class FixtureGraphifier:
    def __init__(self, graph: FileGraph) -> None:
        self.calls = 0
        self._graph = graph

    async def graphify(self, content: NormalizedContent) -> GraphifyResult:
        self.calls += 1
        return GraphifyResult(
            artifacts=file_graph_to_artifacts(self._graph, file_text=content.text),
            edges=file_graph_to_edges(self._graph),
        )


@requires_db
async def test_graphify_fixture_graph_creates_artifacts_and_edges(session: AsyncSession) -> None:
    """PR-06 acceptance: a fixture graph becomes code artifacts with exact spans
    and imports/calls/tests/exposed_as edges; cross-file targets resolve via DB
    lookup; unresolvable targets drop the edge instead of fabricating a node."""
    util_source = SourceItem(
        source_type="github_code",
        source_uri="https://github.com/o/r/blob/sha1/lib/util.py",
        source_version="sha1",
        repo="o/r",
        path="lib/util.py",
        content_hash="util-hash",
    )
    session.add(util_source)
    await session.flush()
    util_file = KnowledgeArtifact(
        artifact_type="code_file",
        source_id=util_source.source_id,
        title="lib/util.py",
        kb_version="v-test.0",
        knowledge_kind="source_backed",
    )
    session.add(util_file)
    await session.commit()

    graph = FileGraph(
        path="a.py",
        symbols=(ParsedSymbol(name="get_user", kind="function", span_start=1, span_end=3),),
        endpoints=(ParsedEndpoint(http_method="GET", route="/users/{id}", symbol="get_user"),),
        tests=(ParsedTest(name="test_get_user", span_start=5, span_end=6, targets=("get_user",)),),
        imports=(
            ParsedImport(target_path="lib/util.py"),  # resolves to the pre-existing artifact
            ParsedImport(target_path="lib/missing.py"),  # unresolved => dropped
        ),
        calls=(
            # cross-file symbol that was never persisted => dropped
            ParsedCall(from_symbol="get_user", to_symbol="lib/util.py::helper"),
        ),
    )
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=SpyWikifier(),
        graphifier=FixtureGraphifier(graph),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run([_connector("line1\nline2\nline3\nline4\nline5\nline6\n")])
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
    symbol = artifacts[("code_symbol", "get_user")]
    assert (symbol.span_start, symbol.span_end) == (1, 3)
    assert symbol.body_text == "line1\nline2\nline3"
    assert symbol.knowledge_kind == "source_backed"
    test_artifact = artifacts[("test", "test_get_user")]
    assert (test_artifact.span_start, test_artifact.span_end) == (5, 6)
    assert test_artifact.body_text == "line5\nline6"
    endpoint = artifacts[("endpoint", "GET /users/{id}")]
    assert endpoint.body_text is None and endpoint.span_start is None
    assert artifacts[("code_file", "a.py")].body_text is None

    edges = (await session.execute(select(KnowledgeEdge))).scalars().all()
    by_type = {edge.edge_type: edge for edge in edges}
    assert sorted(by_type) == ["exposed_as", "imports", "tests"]  # dropped edges absent
    assert all(edge.source == "graphify" and edge.kb_version == "v-test.1" for edge in edges)
    assert by_type["imports"].to_artifact_id == util_file.artifact_id
    assert by_type["imports"].confidence == 1.0
    assert by_type["exposed_as"].from_artifact_id == symbol.artifact_id
    assert by_type["exposed_as"].to_artifact_id == endpoint.artifact_id
    assert by_type["tests"].from_artifact_id == test_artifact.artifact_id
    assert by_type["tests"].to_artifact_id == symbol.artifact_id


class MultiFileGraphifier:
    def __init__(self, graphs: dict[tuple[str | None, str | None], FileGraph]) -> None:
        self.calls = 0
        self._graphs = graphs

    async def graphify(self, content: NormalizedContent) -> GraphifyResult:
        self.calls += 1
        graph = self._graphs[(content.source.repo, content.source.path)]
        return GraphifyResult(
            artifacts=file_graph_to_artifacts(graph, file_text=content.text),
            edges=file_graph_to_edges(graph),
        )


@requires_db
async def test_cross_file_edges_resolve_within_one_build_regardless_of_order(
    session: AsyncSession,
) -> None:
    """a2.py imports b.py and both change in the same build, with a2.py processed
    first. Because edges resolve in one end-of-run pass, the edge must bind to
    b.py's THIS-build artifact — not the stale one from a previous build."""
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

    a_uri = "https://github.com/o/r/blob/sha1/a2.py"
    a_ref = SourceRef(
        source_type="github_code",
        source_uri=a_uri,
        source_version="sha1",
        repo="o/r",
        path="a2.py",
    )
    b_ref = SourceRef(
        source_type="github_code",
        source_uri=b_uri,
        source_version="sha1",
        repo="o/r",
        path="b.py",
    )
    graphs: dict[tuple[str | None, str | None], FileGraph] = {
        ("o/r", "a2.py"): FileGraph(path="a2.py", imports=(ParsedImport(target_path="b.py"),)),
        ("o/r", "b.py"): FileGraph(path="b.py"),
    }
    connector = GitHubCodeConnector(
        FakeBackend([a_ref, b_ref], {a_uri: "import b\n", b_uri: "x = 2\n"})
    )
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=SpyWikifier(),
        graphifier=MultiFileGraphifier(graphs),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run([connector])
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
    """Symbolic keys are repo-relative, so two repos sharing a path must not
    cross-bind: main.py's import of src/utils.py binds to its own repo's
    artifact even though the other repo's src/utils.py was processed first."""
    r_utils_uri = "https://github.com/o/r/blob/sha1/src/utils.py"
    z_utils_uri = "https://github.com/o/z/blob/sha1/src/utils.py"
    z_main_uri = "https://github.com/o/z/blob/sha1/main.py"
    refs = [
        SourceRef(
            source_type="github_code",
            source_uri=r_utils_uri,
            source_version="sha1",
            repo="o/r",
            path="src/utils.py",
        ),
        SourceRef(
            source_type="github_code",
            source_uri=z_main_uri,
            source_version="sha1",
            repo="o/z",
            path="main.py",
        ),
        SourceRef(
            source_type="github_code",
            source_uri=z_utils_uri,
            source_version="sha1",
            repo="o/z",
            path="src/utils.py",
        ),
    ]
    graphs: dict[tuple[str | None, str | None], FileGraph] = {
        ("o/r", "src/utils.py"): FileGraph(path="src/utils.py"),
        ("o/z", "src/utils.py"): FileGraph(path="src/utils.py"),
        ("o/z", "main.py"): FileGraph(
            path="main.py", imports=(ParsedImport(target_path="src/utils.py"),)
        ),
    }
    connector = GitHubCodeConnector(
        FakeBackend(
            refs,
            {r_utils_uri: "r = 1\n", z_utils_uri: "z = 1\n", z_main_uri: "import utils\n"},
        )
    )
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=SpyWikifier(),
        graphifier=MultiFileGraphifier(graphs),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run([connector])
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
async def test_graphify_cache_hit_still_feeds_code_artifacts_to_index(
    session: AsyncSession,
) -> None:
    """A graphify cache hit must surface the mapped code artifact ids to
    embed/index, or code artifacts vanish from Search on every cached build."""
    runner, *_ = _runner(session)
    await runner.run([_connector("x = 1\n")])
    await session.commit()
    code_ids = set(
        (
            await session.execute(
                select(KnowledgeArtifact.artifact_id).where(
                    KnowledgeArtifact.artifact_type.in_(["code_file", "code_symbol"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(code_ids) == 2

    await session.execute(update(SourceItem).values(content_hash="stale"))
    await session.commit()
    runner2, _, graphifier2, _, indexer2 = _runner(session, "v-test.2")
    await runner2.run([_connector("x = 1\n")])
    await session.commit()
    assert graphifier2.calls == 0
    assert code_ids <= set(indexer2.received)
    assert await _count(session, KnowledgeEdge) == 1  # retry never duplicates edges


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
