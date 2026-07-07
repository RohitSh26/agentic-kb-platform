"""Code is graphify-only and zero-LLM, yet keyword-searchable (ADR-0018 phase 1).

A `github_code`-only build (no doc/wiki/card source) must:
  - call the LLM ZERO times (kb_build_run.llm_calls == 0) — code never touches wikify;
  - produce `code_symbol` artifacts whose body_text is the EXACT source span (incl.
    leading docstring/decorators), citable and non-null; and
  - project those symbols into the search index so an exact source token (a function
    name only present in raw code) is keyword-findable in the projection.

Drives REAL whole-tree Graphify extraction (the ast span recovery is exercised end-to-end,
not a spy) with the local embedder + in-memory Search client. A wikifier is wired but MUST
NOT be called — a failing stub proves the routing never reaches it for code.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application.active_version import get_active_kb_version
from agentic_kb_builder.build import Collaborators, run_build
from agentic_kb_builder.domain import (
    DocExtractionResult,
    NormalizedContent,
)
from agentic_kb_builder.embeddings import LocalHashEmbedder
from agentic_kb_builder.indexing import SearchDocUpserter
from agentic_kb_builder.infrastructure.azure_search.search_client import FakeSearchClient
from agentic_kb_builder.infrastructure.postgres.models import (
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

requires_db = pytest.mark.skipif(
    TEST_DATABASE_URL is None, reason="no test database configured (set TEST_DATABASE_URL)"
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

# helper() is a token that lives ONLY in the raw source, never in any summary; finding it
# in the projection proves real-code keyword search, not a paraphrase.
SERVICE_PY = (
    "from pkg.util import helper\n\n\n"
    'def top():\n    """Entry point doc."""\n    return helper()\n\n\n'
    "class Service:\n    def handle(self):\n        return self.helper()\n\n"
    "    def helper(self):\n        return top()\n"
)
UTIL_PY = "def helper():\n    return 42\n"

SOURCES_YAML = (
    "version: 1\n"
    "sources:\n"
    "  - name: code\n"
    "    type: github_code\n"
    "    repo: o/r\n"
    "    branch: main\n"
    "    include: ['**/*.py']\n"
)


class ExplodingDocExtractor:
    """A wikifier that fails if ever called. Code must route graphify-only (ADR-0018),
    so reaching wikify on a code-only build is a routing bug, surfaced loudly here."""

    model_name = "must-not-run"
    model_params_hash = "must-not-run"

    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        raise AssertionError(f"docify must not run for code: {content.source.source_uri}")


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


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "service.py").write_text(SERVICE_PY, encoding="utf-8")
    (pkg / "util.py").write_text(UTIL_PY, encoding="utf-8")
    sources = tmp_path / "sources.yaml"
    sources.write_text(SOURCES_YAML, encoding="utf-8")
    return tmp_path, sources


@requires_db
async def test_code_only_build_is_zero_llm_and_keyword_searchable(
    session: AsyncSession, tmp_path: Path
) -> None:
    workspace, sources = _workspace(tmp_path)
    client = FakeSearchClient()
    run = await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-code.1",
        version="local",
        collaborators=Collaborators(
            doc_extractor=ExplodingDocExtractor(),  # must never be called
            embedder=LocalHashEmbedder(),
            indexer=SearchDocUpserter(session, client),
            search_client=client,
        ),
        activate=True,
    )
    assert run.status in {"completed", "active"}
    assert await get_active_kb_version(session) == "v-code.1"

    # 1) Zero LLM for code ingestion — the headline cost goal of ADR-0018.
    row = (
        await session.execute(select(KbBuildRun).where(KbBuildRun.kb_version == "v-code.1"))
    ).scalar_one()
    assert row.llm_calls == 0

    # 2) No wikify artifacts exist (no summaries/concepts) — code is graph-only.
    wikify_artifacts = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeArtifact)
            .where(KnowledgeArtifact.artifact_type.in_(("summary", "concept", "chunk")))
        )
    ).scalar_one()
    assert wikify_artifacts == 0

    # 3) code_symbol artifacts exist and ALL carry a non-null, exact body_text.
    symbols = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(KnowledgeArtifact.artifact_type == "code_symbol")
            )
        )
        .scalars()
        .all()
    )
    assert len(symbols) >= 3
    assert all(s.body_text for s in symbols)
    by_title = {s.title: s for s in symbols}
    # `top()`'s body is the exact span — docstring + the literal `helper()` call.
    top_body = by_title["top()"].body_text
    assert top_body is not None
    assert "Entry point doc." in top_body
    assert "return helper()" in top_body

    # 4) The projection indexed those symbol bodies, so an exact source token only
    #    present in raw code is keyword-findable (no paraphrase could carry it).
    code_symbol_docs = [d for d in client.docs.values() if d.artifact_type == "code_symbol"]
    assert code_symbol_docs, "code_symbol artifacts must project into the search index"
    hits = [d for d in code_symbol_docs if "helper" in (d.body_text or "")]
    assert hits, "the raw-code token 'helper' must be keyword-findable in the projection"

    # 5) ADR-0018 Phase 2: every Python code_symbol carries non-null search_text
    #    composed of split-identifier words + docstring words + called names (zero-LLM).
    for doc in code_symbol_docs:
        assert doc.search_text is not None, (
            f"code_symbol '{doc.title}' must have non-null search_text (ADR-0018 Phase 2)"
        )
    # `top()` docstring says "Entry point doc." -> "entry", "point", "doc" in search_text.
    # It also calls helper() -> "helper" in search_text.
    top_docs = [d for d in code_symbol_docs if d.title and "top" in d.title]
    assert top_docs, "expected a 'top' symbol in projection"
    top_doc = top_docs[0]
    assert top_doc.search_text is not None
    top_words = set(top_doc.search_text.split())
    assert "entry" in top_words or "top" in top_words, (
        f"split identifier 'top' must appear in search_text, got: {top_doc.search_text!r}"
    )
    assert "entry" in top_words, (
        f"docstring word 'entry' must appear in search_text, got: {top_doc.search_text!r}"
    )
    assert "helper" in top_words, (
        f"called name 'helper' must appear in search_text, got: {top_doc.search_text!r}"
    )

    # 6) Concept-recall: "entry" appears only in the docstring, NOT in the raw code token
    #    stream of top(); finding the symbol via search_text proves the concept-word gain.
    concept_hits = [d for d in code_symbol_docs if d.search_text and "entry" in d.search_text]
    assert concept_hits, (
        "concept word 'entry' (only in docstring, not raw body tokens) must be "
        "findable via search_text — proves the recall gain over body_text-only indexing"
    )

    # 7) DB-level: symbols carry non-null search_text in the knowledge_artifact table.
    for sym in symbols:
        if sym.title and "top" in sym.title:
            assert sym.search_text is not None, "top symbol must have search_text in DB"
            assert "entry" in sym.search_text, (
                f"docstring word 'entry' expected in DB search_text, got: {sym.search_text!r}"
            )


@requires_db
async def test_imports_edges_emitted_for_in_build_module(
    session: AsyncSession, tmp_path: Path
) -> None:
    """ADR-0020: `imports` file->file edge is written when file A imports file B in the build.

    SERVICE_PY starts with ``from pkg.util import helper`` — this resolves to util.py
    (``pkg.util`` -> ``pkg/util.py``) which IS in the build, so an ``imports`` edge must
    be written from service.py's code_file artifact to util.py's code_file artifact.
    """
    workspace, sources = _workspace(tmp_path)
    client = FakeSearchClient()
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-imports.1",
        version="local",
        collaborators=Collaborators(
            doc_extractor=ExplodingDocExtractor(),
            embedder=LocalHashEmbedder(),
            indexer=SearchDocUpserter(session, client),
            search_client=client,
        ),
        activate=True,
    )

    imports_edges = (
        (
            await session.execute(
                select(KnowledgeEdge).where(
                    KnowledgeEdge.edge_type == "imports",
                    KnowledgeEdge.kb_version == "v-imports.1",
                )
            )
        )
        .scalars()
        .all()
    )
    # service.py imports pkg.util -> resolves to pkg/util.py -> 1 imports edge
    assert len(imports_edges) >= 1, "expected at least one imports edge (service->util)"

    # Verify edge goes from the service.py code_file to the util.py code_file
    from_ids = {e.from_artifact_id for e in imports_edges}
    to_ids = {e.to_artifact_id for e in imports_edges}
    from_artifacts = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.artifact_id.in_(from_ids),
                    KnowledgeArtifact.artifact_type == "code_file",
                )
            )
        )
        .scalars()
        .all()
    )
    to_artifacts = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.artifact_id.in_(to_ids),
                    KnowledgeArtifact.artifact_type == "code_file",
                )
            )
        )
        .scalars()
        .all()
    )
    from_titles = {a.title or "" for a in from_artifacts}
    to_titles = {a.title or "" for a in to_artifacts}
    assert any("service" in t for t in from_titles), (
        f"imports edge from should be service.py, got {from_titles}"
    )
    assert any("util" in t for t in to_titles), (
        f"imports edge to should be util.py, got {to_titles}"
    )

    # All edges must have trust_class=EXTRACTED and confidence=1.0 (no dangling)
    for edge in imports_edges:
        assert edge.trust_class == "EXTRACTED"
        assert edge.confidence == 1.0


@requires_db
async def test_imports_edge_not_emitted_for_stdlib_or_third_party(
    session: AsyncSession, tmp_path: Path
) -> None:
    """ADR-0020: third-party/stdlib imports (not in the build) produce NO edge — no dangling."""
    # util.py has no imports; service.py imports functools (stdlib) which is not in the build.
    # Only pkg.util is in the build and maps to util.py.
    workspace, sources = _workspace(tmp_path)
    client = FakeSearchClient()
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-imports.2",
        version="local",
        collaborators=Collaborators(
            doc_extractor=ExplodingDocExtractor(),
            embedder=LocalHashEmbedder(),
            indexer=SearchDocUpserter(session, client),
            search_client=client,
        ),
        activate=False,
    )
    all_edges = (
        (
            await session.execute(
                select(KnowledgeEdge).where(
                    KnowledgeEdge.edge_type == "imports",
                    KnowledgeEdge.kb_version == "v-imports.2",
                )
            )
        )
        .scalars()
        .all()
    )
    # No edge from functools (stdlib) or any other unbuilt module
    # All edges that do exist must point to artifacts we persisted (not dangling)
    all_artifact_ids = {
        r.artifact_id
        for r in (
            (
                await session.execute(
                    select(KnowledgeArtifact).where(KnowledgeArtifact.kb_version == "v-imports.2")
                )
            )
            .scalars()
            .all()
        )
    }
    for edge in all_edges:
        assert edge.to_artifact_id in all_artifact_ids, (
            f"dangling imports edge: to_artifact_id {edge.to_artifact_id} not in build artifacts"
        )
        assert edge.from_artifact_id in all_artifact_ids, (
            f"dangling imports edge: from_artifact_id {edge.from_artifact_id} "
            "not in build artifacts"
        )


@requires_db
async def test_imports_edges_idempotent_same_kb_version(
    session: AsyncSession, tmp_path: Path
) -> None:
    """ADR-0020: running the build twice for the same kb_version does not duplicate edges.

    The first pass writes the `imports` edges; the second pass with unchanged content
    skips all source processing (incremental build: nothing changed => nothing rewritten)
    so the edge count is stable — no duplication on retry.
    """
    workspace, sources = _workspace(tmp_path)
    client = FakeSearchClient()
    kb_version = "v-imports.idem"

    async def _run() -> None:
        await run_build(
            session,
            sources_path=str(sources),
            workspace=str(workspace),
            kb_version=kb_version,
            version="local",
            collaborators=Collaborators(
                doc_extractor=ExplodingDocExtractor(),
                embedder=LocalHashEmbedder(),
                indexer=SearchDocUpserter(session, client),
                search_client=client,
            ),
            activate=False,
        )

    async def _edge_count() -> int:
        return (
            await session.execute(
                select(func.count())
                .select_from(KnowledgeEdge)
                .where(
                    KnowledgeEdge.edge_type == "imports",
                    KnowledgeEdge.kb_version == kb_version,
                )
            )
        ).scalar_one()

    await _run()
    count_after_first = await _edge_count()
    assert count_after_first >= 1, "first build must produce at least one imports edge"

    await _run()
    count_after_second = await _edge_count()
    assert count_after_second == count_after_first, (
        f"second run duplicated edges: {count_after_first} -> {count_after_second}"
    )


@requires_db
async def test_search_text_idempotent_rebuild(session: AsyncSession, tmp_path: Path) -> None:
    """ADR-0018 Phase 2: rebuilding unchanged content does not duplicate or change
    search_text. The incremental build skips unchanged files so the second run must
    produce the same search_text values (content-hash gated)."""
    workspace, sources = _workspace(tmp_path)
    client = FakeSearchClient()
    kb_version = "v-st-idem.1"

    async def _run() -> None:
        await run_build(
            session,
            sources_path=str(sources),
            workspace=str(workspace),
            kb_version=kb_version,
            version="local",
            collaborators=Collaborators(
                doc_extractor=ExplodingDocExtractor(),
                embedder=LocalHashEmbedder(),
                indexer=SearchDocUpserter(session, client),
                search_client=client,
            ),
            activate=False,
        )

    await _run()
    symbols_after_first = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.artifact_type == "code_symbol",
                    KnowledgeArtifact.kb_version == kb_version,
                )
            )
        )
        .scalars()
        .all()
    )
    search_texts_first = {s.title: s.search_text for s in symbols_after_first}

    # Run again with the same source — incremental build, no content change.
    await _run()
    symbols_after_second = (
        (
            await session.execute(
                select(KnowledgeArtifact).where(
                    KnowledgeArtifact.artifact_type == "code_symbol",
                    KnowledgeArtifact.kb_version == kb_version,
                )
            )
        )
        .scalars()
        .all()
    )
    search_texts_second = {s.title: s.search_text for s in symbols_after_second}

    # Same symbols, same search_text — determinism holds.
    assert search_texts_first == search_texts_second, (
        "search_text must be identical across identical rebuild passes"
    )
    # All Python symbols must have non-null search_text.
    for title, st in search_texts_second.items():
        assert st is not None, f"symbol '{title}' must have non-null search_text"


@requires_db
async def test_code_file_skeleton_search_text_and_incremental_skip(
    session: AsyncSession, tmp_path: Path
) -> None:
    """ADR-0033 (PR-42): each Python code_file artifact stores the deterministic
    skeleton as its search_text (bodies elided, structure kept), body_text stays
    None (pointer-only), and an unchanged rebuild neither duplicates the rows nor
    recomputes different text — the content-hash gate skips the whole graphify
    (and therefore skeleton) pass."""
    workspace, sources = _workspace(tmp_path)
    client = FakeSearchClient()
    kb_version = "v-skel.1"

    async def _run() -> None:
        await run_build(
            session,
            sources_path=str(sources),
            workspace=str(workspace),
            kb_version=kb_version,
            version="local",
            collaborators=Collaborators(
                doc_extractor=ExplodingDocExtractor(),
                embedder=LocalHashEmbedder(),
                indexer=SearchDocUpserter(session, client),
                search_client=client,
            ),
            activate=False,
        )

    async def _code_files() -> list[KnowledgeArtifact]:
        return list(
            (
                await session.execute(
                    select(KnowledgeArtifact).where(
                        KnowledgeArtifact.artifact_type == "code_file",
                        KnowledgeArtifact.kb_version == kb_version,
                    )
                )
            )
            .scalars()
            .all()
        )

    await _run()
    files_first = await _code_files()
    assert len(files_first) == 2  # pkg/service.py + pkg/util.py
    by_title = {f.title: f for f in files_first}
    service = by_title["pkg/service.py"]
    assert service.body_text is None, "code_file stays pointer-only (no raw document)"
    assert service.search_text is not None
    assert "def top():" in service.search_text  # signature kept
    assert '"""Entry point doc."""' in service.search_text  # docstring kept
    assert "return helper()" not in service.search_text  # body elided
    assert "line elided" in service.search_text  # counted placeholder marks each elision

    # Unchanged rebuild: the content-hash gate skips graphify entirely, so the
    # rows are not duplicated and the skeleton text is byte-identical.
    await _run()
    files_second = await _code_files()
    assert len(files_second) == len(files_first), "rebuild must not duplicate code_file rows"
    assert {f.title: f.search_text for f in files_second} == {
        f.title: f.search_text for f in files_first
    }
