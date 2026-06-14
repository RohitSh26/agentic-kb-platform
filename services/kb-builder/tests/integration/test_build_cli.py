"""End-to-end `build` CLI test (PR-22): a local workspace -> Postgres, no cloud.

Drives `run_build` over a tiny fixture workspace with the REAL Graphify extractor and
the local embedder + in-memory Search indexer (wikify is faked so the test needs no LLM).
Asserts the build produces code artifacts/edges, activates the kb_version after the
consistency gate, and is incremental (a re-run on unchanged content writes nothing new).
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
from agentic_kb_builder.infrastructure.postgres.models import KnowledgeArtifact, KnowledgeEdge

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

SERVICE_PY = (
    "from pkg.util import helper\n\n\n"
    "def top():\n    return helper()\n\n\n"
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


class FakeWikifier:
    model_name = "fake-wikify"
    model_params_hash = "fake-params"

    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
        return [
            WikifyArtifactDraft(
                artifact_type="summary",
                knowledge_kind="interpreted",
                title=f"summary of {content.source.path}",
                body_text=f"Summary of {content.source.path}",
                authority_score=0.5,
                freshness_score=1.0,
            )
        ]


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
    pkg.mkdir()
    (pkg / "service.py").write_text(SERVICE_PY, encoding="utf-8")
    (pkg / "util.py").write_text(UTIL_PY, encoding="utf-8")
    sources = tmp_path / "sources.yaml"
    sources.write_text(SOURCES_YAML, encoding="utf-8")
    return tmp_path, sources


def _collaborators(session: AsyncSession) -> Collaborators:
    client = FakeSearchClient()
    return Collaborators(
        wikifier=FakeWikifier(),
        graphifier=GraphifyGraphifier(),
        embedder=LocalHashEmbedder(),
        indexer=SearchDocUpserter(session, client),
        search_client=client,
    )


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@requires_db
async def test_build_creates_artifacts_and_activates(session: AsyncSession, tmp_path: Path) -> None:
    workspace, sources = _workspace(tmp_path)
    run = await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-cli.1",
        version="local",
        collaborators=_collaborators(session),
        activate=True,
    )
    # activation may flip the in-session instance to "active"; both mean the build succeeded.
    assert run.status in {"completed", "active"}
    assert await get_active_kb_version(session) == "v-cli.1"

    files = await session.execute(
        select(func.count())
        .select_from(KnowledgeArtifact)
        .where(KnowledgeArtifact.artifact_type == "code_file")
    )
    symbols = await session.execute(
        select(func.count())
        .select_from(KnowledgeArtifact)
        .where(KnowledgeArtifact.artifact_type == "code_symbol")
    )
    assert files.scalar_one() == 2  # service.py + util.py
    assert symbols.scalar_one() >= 3  # top, Service, methods, helper
    assert await _count(session, KnowledgeEdge) >= 1


@requires_db
async def test_build_is_incremental_on_rerun(session: AsyncSession, tmp_path: Path) -> None:
    workspace, sources = _workspace(tmp_path)
    collab = _collaborators(session)
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-cli.1",
        version="local",
        collaborators=collab,
        activate=False,
    )
    artifacts_after_first = await _count(session, KnowledgeArtifact)

    # Unchanged content -> generation cache hits -> no new artifacts written.
    await run_build(
        session,
        sources_path=str(sources),
        workspace=str(workspace),
        kb_version="v-cli.2",
        version="local",
        collaborators=collab,
        activate=False,
    )
    assert await _count(session, KnowledgeArtifact) == artifacts_after_first
