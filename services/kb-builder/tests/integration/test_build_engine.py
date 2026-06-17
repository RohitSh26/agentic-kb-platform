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
    chunk_summary_cache_key,
    code_graph_cache_key,
    get_active_kb_version,
)
from agentic_kb_builder.connectors import GitHubCodeConnector, GitHubDocConnector
from agentic_kb_builder.domain import (
    Chunk,
    ConceptDraft,
    NormalizedContent,
    SourceBackedFactDraft,
    SourceRef,
    WikifyArtifactDraft,
    WikifyGeneration,
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
from agentic_kb_builder.wikify import WikifyGenerator

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

Spies = tuple[BuildRunner, SpyWikifier, SpyEmbedder, SpyIndexer]

# A one-function code file. Whole-tree Graphify yields a code_file artifact plus a
# code_symbol (`get_user`, span 1-2, body_text=the def line) and one `defined_in` edge.
CODE_FN = "def get_user():\n    return 1\n"


def _connector(raw: str) -> GitHubCodeConnector:
    return GitHubCodeConnector(FakeBackend([REF], {URI: raw}))


def _doc_connector(raw: str) -> GitHubDocConnector:
    """A github_doc connector for wikify-pipeline tests (graphify never runs on it)."""
    return GitHubDocConnector(FakeBackend([DOC_REF], {DOC_URI: raw}))


def _runner(session: AsyncSession, kb_version: str = "v-test.1") -> Spies:
    wikifier = SpyWikifier()
    embedder = SpyEmbedder()
    indexer = SpyIndexer()
    runner = BuildRunner(
        session,
        kb_version=kb_version,
        wikifier=wikifier,
        embedder=embedder,
        indexer=indexer,
    )
    return runner, wikifier, embedder, indexer


async def _count(session: AsyncSession, model: type[KnowledgeArtifact] | type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@requires_db
async def test_first_build_processes_then_unchanged_build_skips_everything(
    session: AsyncSession,
) -> None:
    # A github_doc source (ADR-0018): wikify runs, graphify never does.
    runner, wikifier, embedder, indexer = _runner(session)
    run1 = await runner.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run1.sources_seen == 1
    assert run1.sources_changed == 1
    assert run1.llm_calls == 1
    # only the summary body is embedded (no code source ⇒ no graphify artifacts)
    assert run1.embedding_calls == 1
    assert (wikifier.calls, embedder.calls, indexer.calls) == (1, 1, 1)

    seen_before = (await session.execute(select(SourceItem.last_seen_at))).scalar_one()
    assert seen_before is not None

    runner2, wikifier2, embedder2, indexer2 = _runner(session, "v-test.2")
    run2 = await runner2.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run2.sources_seen == 1
    assert run2.sources_changed == 0
    assert run2.llm_calls == 0
    assert run2.embedding_calls == 0
    # unchanged content_hash => chunk/wikify/graphify/embed/index all skipped
    assert (wikifier2.calls, embedder2.calls, indexer2.calls) == (0, 0, 0)
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

    runner2, wikifier2, embedder2, indexer2 = _runner(session, "v-test.2")
    run2 = await runner2.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run2.sources_changed == 1
    assert run2.llm_calls == 0
    assert run2.embedding_calls == 0
    assert (wikifier2.calls, embedder2.calls) == (0, 0)
    assert indexer2.calls == 1  # reindexing reused artifacts is allowed
    # just the wikify summary — no duplicates on retry; doc sources have no edges
    assert await _count(session, KnowledgeArtifact) == 1
    assert await _count(session, KnowledgeEdge) == 0


@requires_db
async def test_failed_wikify_leaves_no_cache_row_or_artifacts_but_audit_row_survives(
    session: AsyncSession,
) -> None:
    runner = BuildRunner(
        session,
        kb_version="v-test.1",
        wikifier=FailingWikifier(),
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    with pytest.raises(RuntimeError, match="model exploded"):
        # wikify runs only for prose now (ADR-0018) — a github_doc source.
        await runner.run([_doc_connector("Some prose to summarize.\n")])
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
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    await runner.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    # 2 wikify concepts (a github_doc source ⇒ no graphify artifacts)
    assert await _count(session, KnowledgeArtifact) == 2
    # one wikify cache row (no graphify cache row for a doc source)
    assert await _count(session, GenerationCache) == 1
    assert await _count(session, GenerationCacheArtifact) == 2
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
        embedder=embedder2,
        indexer=indexer2,
    )
    run2 = await runner2.run([_doc_connector("Some prose to summarize.\n")])
    await session.commit()
    assert run2.llm_calls == 0
    assert wikifier2.calls == 0
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
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    run1 = await runner.run([_doc_connector(raw)])
    await session.commit()
    assert model_client.calls == 1
    assert run1.llm_calls == 1

    artifacts = (await session.execute(select(KnowledgeArtifact))).scalars().all()
    by_type = {artifact.artifact_type: artifact for artifact in artifacts}
    # a github_doc source (ADR-0018) ⇒ wikify only: 1 chunk + summary + concept +
    # the quote-backed fact (the invented fact is dropped); no graphify artifacts.
    assert sorted(by_type) == [
        "chunk",
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
        embedder=SpyEmbedder(),
        indexer=SpyIndexer(),
    )
    run2 = await runner2.run([_doc_connector(raw)])
    await session.commit()
    assert model_client2.calls == 0
    assert run2.llm_calls == 0
    assert await _count(session, KnowledgeArtifact) == 4


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
        wikifier=SpyWikifier(),
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
        wikifier=SpyWikifier(),
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
        wikifier=SpyWikifier(),
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
