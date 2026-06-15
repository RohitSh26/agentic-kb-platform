"""Code is graphify-only and zero-LLM, yet keyword-searchable (ADR-0018 phase 1).

A `github_code`-only build (no doc/wiki/card source) must:
  - call the LLM ZERO times (kb_build_run.llm_calls == 0) — code never touches wikify;
  - produce `code_symbol` artifacts whose body_text is the EXACT source span (incl.
    leading docstring/decorators), citable and non-null; and
  - project those symbols into the search index so an exact source token (a function
    name only present in raw code) is keyword-findable in the projection.

Drives the REAL GraphifyGraphifier (the ast span recovery is exercised end-to-end, not
a spy) with the local embedder + in-memory Search client. A wikifier is wired but MUST
NOT be called — a failing stub proves the routing never reaches it for code.
"""

import os
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application.active_version import get_active_kb_version
from agentic_kb_builder.build import Collaborators, run_build
from agentic_kb_builder.domain import NormalizedContent, WikifyArtifactDraft
from agentic_kb_builder.embeddings import LocalHashEmbedder
from agentic_kb_builder.graphify import GraphifyGraphifier
from agentic_kb_builder.indexing import SearchDocUpserter
from agentic_kb_builder.infrastructure.azure_search.search_client import FakeSearchClient
from agentic_kb_builder.infrastructure.postgres.models import KbBuildRun, KnowledgeArtifact

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


class ExplodingWikifier:
    """A wikifier that fails if ever called. Code must route graphify-only (ADR-0018),
    so reaching wikify on a code-only build is a routing bug, surfaced loudly here."""

    model_name = "must-not-run"
    model_params_hash = "must-not-run"

    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
        raise AssertionError(f"wikify must not run for code: {content.source.source_uri}")


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
            wikifier=ExplodingWikifier(),  # must never be called
            graphifier=GraphifyGraphifier(),
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
