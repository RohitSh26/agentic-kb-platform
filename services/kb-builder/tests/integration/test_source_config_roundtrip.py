"""Round-trip: sources.yaml -> connectors -> build run -> acl_teams on source_item.

Same DB policy as the build engine tests: skipped gracefully when
TEST_DATABASE_URL is not configured. Pipeline stages are minimal fakes — the
assertion is about config-driven filtering and ACL flow, not pipeline output.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_kb_builder.application import BuildRunner, EmbeddingResult
from agentic_kb_builder.connectors import connectors_from_config, load_source_config
from agentic_kb_builder.connectors.source_connector import FetchBackend
from agentic_kb_builder.domain import (
    NormalizedContent,
    SourceRef,
    SourceSpec,
    WikifyArtifactDraft,
)
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.infrastructure.postgres.models import SourceItem

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


YAML = """
version: 1
defaults:
  acl_teams: ["org-readers"]
sources:
  - name: handbook
    type: github_doc
    repo: o/r
    include: ["docs/**/*.md"]
    exclude: ["docs/archive/**"]
    acl_teams: ["docs-team"]
"""


def _ref(path: str) -> SourceRef:
    return SourceRef(
        source_type="github_doc",
        source_uri=f"https://github.com/o/r/blob/sha1/{path}",
        source_version="sha1",
        repo="o/r",
        branch="main",
        path=path,
    )


class FakeBackend:
    def __init__(self, sources: list[SourceRef], texts: dict[str, str]) -> None:
        self._sources = sources
        self._texts = texts
        self.fetched: list[str] = []

    async def list_sources(self) -> list[SourceRef]:
        return self._sources

    async def fetch_text(self, source: SourceRef) -> str:
        self.fetched.append(source.source_uri)
        return self._texts[source.source_uri]


class FakeWikifier:
    model_name = "gpt-test"
    model_params_hash = "params-test"

    async def wikify(self, content: NormalizedContent) -> Sequence[WikifyArtifactDraft]:
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


class FakeEmbedder:
    embedding_model = "embed-test"

    async def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(embedding_hash="emb-" + content_hash(text)[:12], vector=[0.5])


class FakeIndexer:
    async def upsert_documents(self, artifact_ids: Sequence[uuid.UUID]) -> int:
        return len(artifact_ids)

    async def delete_orphaned(self) -> int:
        return 0

    async def reconcile_missing(self) -> int:
        return 0


def _runner(session: AsyncSession, kb_version: str) -> BuildRunner:
    return BuildRunner(
        session,
        kb_version=kb_version,
        wikifier=FakeWikifier(),
        embedder=FakeEmbedder(),
        indexer=FakeIndexer(),
    )


@requires_db
async def test_yaml_to_connectors_to_build_run_with_acl_and_filtering(
    session: AsyncSession, tmp_path: Path
) -> None:
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(YAML, encoding="utf-8")
    config = load_source_config(config_path)

    kept = _ref("docs/guide.md")
    excluded = _ref("docs/archive/old.md")
    backend = FakeBackend([kept, excluded], {kept.source_uri: "# Guide\n"})

    def factory(spec: SourceSpec, token: str | None) -> FetchBackend:
        assert token is None  # no auth configured for this source
        return backend

    connectors = connectors_from_config(config, factory)
    run = await _runner(session, "v-cfg.1").run(connectors)
    await session.commit()

    # the excluded path was never fetched, hashed, or stored
    assert run.sources_seen == 1
    assert backend.fetched == [kept.source_uri]
    rows = (await session.execute(select(SourceItem))).scalars().all()
    assert [row.path for row in rows] == ["docs/guide.md"]

    # explicit per-source acl_teams beat the defaults and land on insert
    assert rows[0].acl_teams == ["docs-team"]


@requires_db
async def test_acl_teams_updated_on_existing_source_item(
    session: AsyncSession, tmp_path: Path
) -> None:
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(YAML, encoding="utf-8")
    config = load_source_config(config_path)

    ref = _ref("docs/guide.md")
    backend_v1 = FakeBackend([ref], {ref.source_uri: "v1\n"})
    connectors = connectors_from_config(config, lambda spec, token: backend_v1)
    await _runner(session, "v-cfg.1").run(connectors)
    await session.commit()

    # same source re-ingested with changed content and a changed ACL set
    retitled = YAML.replace('["docs-team"]', '["docs-team", "auditors"]')
    config_path.write_text(retitled, encoding="utf-8")
    config2 = load_source_config(config_path)
    backend_v2 = FakeBackend([ref], {ref.source_uri: "v2 changed\n"})
    connectors2 = connectors_from_config(config2, lambda spec, token: backend_v2)
    await _runner(session, "v-cfg.2").run(connectors2)
    await session.commit()

    rows = (await session.execute(select(SourceItem))).scalars().all()
    assert len(rows) == 1  # natural identity: upsert, not duplicate
    assert rows[0].acl_teams == ["docs-team", "auditors"]


@requires_db
async def test_acl_only_change_lands_even_when_content_is_unchanged(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Removing or adding a team is an access change; it must not be gated
    behind the content-hash skip."""
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(YAML, encoding="utf-8")
    config = load_source_config(config_path)

    ref = _ref("docs/guide.md")
    backend = FakeBackend([ref], {ref.source_uri: "same content\n"})
    await _runner(session, "v-cfg.1").run(connectors_from_config(config, lambda s, t: backend))
    await session.commit()

    revoked = YAML.replace('["docs-team"]', "[]")
    config_path.write_text(revoked, encoding="utf-8")
    config2 = load_source_config(config_path)
    backend2 = FakeBackend([ref], {ref.source_uri: "same content\n"})
    run2 = await _runner(session, "v-cfg.2").run(
        connectors_from_config(config2, lambda s, t: backend2)
    )
    await session.commit()

    assert run2.sources_changed == 0  # the hash gate still skipped the pipeline
    row = (await session.execute(select(SourceItem))).scalars().one()
    assert row.acl_teams == []
