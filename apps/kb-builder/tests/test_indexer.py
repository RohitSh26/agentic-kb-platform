"""Search indexer tests (PR-08 acceptance criteria).

All tests run against the in-memory FakeSearchClient — no Azure. DB-backed
tests cover projection mapping, the changed-docs-only upsert, orphan
reconciliation, and the drift consistency check failing on injected drift;
they skip gracefully when TEST_DATABASE_URL is not configured, same policy as
the other kb-builder suites.
"""

import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from common.hashing import content_hash
from common.search.client import FakeSearchClient
from db.models import EmbeddingCache, KnowledgeArtifact, SourceItem
from kb_builder.build import EmbeddingCacheGate
from kb_builder.indexer import (
    SearchDocUpserter,
    delete_orphaned_docs,
    load_search_docs,
    make_consistency_validator,
)

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

EMBEDDING_MODEL = "fake-embed-1"
VECTOR = [0.1, 0.2, 0.3]


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    assert TEST_DATABASE_URL is not None
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    # alembic's fileConfig() disables already-created loggers; re-enable the
    # indexer loggers so caplog assertions on structured events still work.
    for name in (
        "kb_builder.indexer.projection",
        "kb_builder.indexer.upsert",
        "kb_builder.indexer.consistency",
    ):
        logging.getLogger(name).disabled = False
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


async def _add_source(
    session: AsyncSession, source_type: str, source_uri: str, *, is_deleted: bool = False
) -> SourceItem:
    source = SourceItem(
        source_type=source_type,
        source_uri=source_uri,
        source_version="1",
        content_hash=f"hash:{source_uri}",
        is_deleted=is_deleted,
    )
    session.add(source)
    await session.flush()
    return source


async def _add_artifact(
    session: AsyncSession,
    *,
    source: SourceItem,
    artifact_type: str,
    title: str | None,
    body_text: str | None,
    knowledge_kind: str | None = None,
    artifact_hash: str | None = None,
) -> KnowledgeArtifact:
    artifact = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source.source_id,
        title=title,
        body_text=body_text,
        kb_version="v-build.1",
        knowledge_kind=knowledge_kind,
        authority_score=0.6,
        freshness_score=1.0,
        artifact_hash=artifact_hash,
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def _add_embedding(
    session: AsyncSession,
    artifact: KnowledgeArtifact,
    *,
    text_hash: str | None = None,
    embedding: list[float] | None = None,
) -> EmbeddingCache:
    assert artifact.body_text is not None
    row = EmbeddingCache(
        artifact_id=artifact.artifact_id,
        text_hash=text_hash or content_hash(artifact.body_text),
        embedding_model=EMBEDDING_MODEL,
        embedding_hash=f"ehash:{artifact.artifact_id}",
        embedding=embedding,
    )
    session.add(row)
    await session.flush()
    return row


async def _seed_concept(session: AsyncSession) -> KnowledgeArtifact:
    wiki = await _add_source(session, "azure_wiki", "wiki://embeddings")
    return await _add_artifact(
        session,
        source=wiki,
        artifact_type="concept",
        title="User Embeddings",
        body_text="Per-user vectors and how they are refreshed.",
        knowledge_kind="interpreted",
        artifact_hash="ahash:concept",
    )


# ---------------------------------------------------------------------------
# Projection mapping
# ---------------------------------------------------------------------------


@requires_db
async def test_projection_maps_artifact_fields(session: AsyncSession) -> None:
    concept = await _seed_concept(session)
    await _add_embedding(session, concept, embedding=VECTOR)
    code = await _add_source(session, "github_code", "github://org/repo/src/service.py")
    # pointer-only artifacts are not projectable
    await _add_artifact(
        session, source=code, artifact_type="code_file", title="src/service.py", body_text=None
    )
    # deleted sources are not projectable
    gone = await _add_source(session, "azure_wiki", "wiki://gone", is_deleted=True)
    await _add_artifact(
        session, source=gone, artifact_type="concept", title="Old Concept", body_text="text"
    )

    docs = await load_search_docs(session)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.doc_id == str(concept.artifact_id)
    assert doc.artifact_id == concept.artifact_id
    assert doc.artifact_type == "concept"
    assert doc.source_type == "azure_wiki"
    assert doc.source_uri == "wiki://embeddings"
    assert doc.title == "User Embeddings"
    assert doc.body_text == concept.body_text
    assert doc.kb_version == "v-build.1"
    assert doc.knowledge_kind == "interpreted"
    assert doc.authority_score == pytest.approx(0.6)
    assert doc.freshness_score == pytest.approx(1.0)
    assert doc.artifact_hash == "ahash:concept"
    assert doc.embedding == tuple(VECTOR)
    assert doc.embedding_model == EMBEDDING_MODEL


@requires_db
async def test_projection_ignores_stale_embedding_rows(session: AsyncSession) -> None:
    """A cached vector for an older body text never attaches to the new doc."""
    concept = await _seed_concept(session)
    await _add_embedding(session, concept, text_hash="hash-of-old-body", embedding=VECTOR)

    [doc] = await load_search_docs(session)

    assert doc.embedding is None
    assert doc.embedding_model is None


# ---------------------------------------------------------------------------
# Changed-docs-only upsert
# ---------------------------------------------------------------------------


@requires_db
async def test_upsert_writes_only_requested_artifacts(session: AsyncSession) -> None:
    changed = await _seed_concept(session)
    await _add_embedding(session, changed, embedding=VECTOR)
    other_source = await _add_source(session, "github_doc", "github://org/repo/README.md")
    unchanged = await _add_artifact(
        session,
        source=other_source,
        artifact_type="chunk",
        title="README intro",
        body_text="Intro text.",
        knowledge_kind="source_backed",
        artifact_hash="ahash:chunk",
    )

    client = FakeSearchClient()
    upserter = SearchDocUpserter(session, client)
    upserted = await upserter.upsert_documents([changed.artifact_id])

    assert upserted == 1
    assert set(client.docs) == {str(changed.artifact_id)}
    assert str(unchanged.artifact_id) not in client.docs
    # rerun with the same ids replaces in place — never duplicates
    assert await upserter.upsert_documents([changed.artifact_id]) == 1
    assert len(client.docs) == 1


@requires_db
async def test_upsert_partial_failure_fails_the_run(session: AsyncSession) -> None:
    """A doc the index silently dropped would wedge validation on every later
    build, so an incomplete upsert must raise (the runner then marks the run
    failed and the unchanged-skip never commits — next build retries)."""

    class DroppingSearchClient(FakeSearchClient):
        async def upsert_docs(self, docs):  # type: ignore[no-untyped-def]
            await super().upsert_docs(docs)
            return len(docs) - 1

    concept = await _seed_concept(session)
    await _add_embedding(session, concept, embedding=VECTOR)

    upserter = SearchDocUpserter(session, DroppingSearchClient())
    with pytest.raises(RuntimeError, match="search upsert incomplete"):
        await upserter.upsert_documents([concept.artifact_id])

    # the failed batch must not be stamped as indexed
    row = (
        await session.execute(
            select(EmbeddingCache).where(EmbeddingCache.artifact_id == concept.artifact_id)
        )
    ).scalar_one()
    assert row.azure_search_doc_id is None


@requires_db
async def test_vectorless_cache_row_is_miss_and_backfills(session: AsyncSession) -> None:
    """Pre-0006 rows have no stored vector and cannot serve an index rebuild:
    the gate treats them as misses and the re-record fills the vector in place
    (never duplicating the row, never erasing it on a vectorless call)."""
    concept = await _seed_concept(session)
    assert concept.body_text is not None
    artifact_id = concept.artifact_id
    text_hash = content_hash(concept.body_text)
    gate = EmbeddingCacheGate(session)
    await gate.record(
        artifact_id=artifact_id,
        text_hash=text_hash,
        embedding_model=EMBEDDING_MODEL,
        embedding_hash="ehash:old",
    )

    miss = await gate.lookup(
        artifact_id=artifact_id, text_hash=text_hash, embedding_model=EMBEDDING_MODEL
    )
    assert miss is None  # vectorless row must not be a hit

    await gate.record(
        artifact_id=artifact_id,
        text_hash=text_hash,
        embedding_model=EMBEDDING_MODEL,
        embedding_hash="ehash:new",
        embedding=VECTOR,
    )
    rows = (await session.execute(select(EmbeddingCache))).scalars().all()
    assert len(rows) == 1 and rows[0].embedding == VECTOR

    # a later vectorless record must leave the row untouched — neither the
    # vector nor its hash may change (hash and vector always move together)
    await gate.record(
        artifact_id=artifact_id,
        text_hash=text_hash,
        embedding_model=EMBEDDING_MODEL,
        embedding_hash="ehash:newer",
    )
    session.expire_all()
    refreshed = await gate.lookup(
        artifact_id=artifact_id, text_hash=text_hash, embedding_model=EMBEDDING_MODEL
    )
    assert refreshed is not None
    assert refreshed.embedding == VECTOR
    assert refreshed.embedding_hash == "ehash:new"


@requires_db
async def test_upsert_records_search_doc_id_on_embedding_row(session: AsyncSession) -> None:
    concept = await _seed_concept(session)
    await _add_embedding(session, concept, embedding=VECTOR)

    await SearchDocUpserter(session, FakeSearchClient()).upsert_documents([concept.artifact_id])

    row = (
        await session.execute(
            select(EmbeddingCache).where(EmbeddingCache.artifact_id == concept.artifact_id)
        )
    ).scalar_one()
    assert row.azure_search_doc_id == str(concept.artifact_id)


# ---------------------------------------------------------------------------
# Orphan reconciliation + drift consistency
# ---------------------------------------------------------------------------


@requires_db
async def test_orphaned_docs_are_deleted(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    concept = await _seed_concept(session)
    client = FakeSearchClient()
    await SearchDocUpserter(session, client).upsert_documents([concept.artifact_id])
    # inject an index doc whose artifact no longer exists in the registry
    [live_doc] = client.docs.values()
    orphan = live_doc.model_copy(update={"doc_id": str(uuid.uuid4()), "artifact_id": uuid.uuid4()})
    client.docs[orphan.doc_id] = orphan

    with caplog.at_level(logging.WARNING, logger="kb_builder.indexer.upsert"):
        removed = await delete_orphaned_docs(session, client)

    assert removed == 1
    assert set(client.docs) == {str(concept.artifact_id)}
    assert any("event=indexer_orphans_deleted" in r.getMessage() for r in caplog.records)
    # nothing left to remove on rerun
    assert await delete_orphaned_docs(session, client) == 0


@requires_db
async def test_consistency_passes_when_index_mirrors_registry(session: AsyncSession) -> None:
    concept = await _seed_concept(session)
    client = FakeSearchClient()
    await SearchDocUpserter(session, client).upsert_documents([concept.artifact_id])

    validate = make_consistency_validator(client)
    assert await validate(session, "v-build.1") is True


@requires_db
@pytest.mark.parametrize("drift_class", ["missing", "orphaned", "drifted"])
async def test_consistency_fails_on_injected_drift(
    session: AsyncSession, caplog: pytest.LogCaptureFixture, drift_class: str
) -> None:
    concept = await _seed_concept(session)
    client = FakeSearchClient()
    await SearchDocUpserter(session, client).upsert_documents([concept.artifact_id])

    if drift_class == "missing":
        client.docs.clear()
    elif drift_class == "orphaned":
        [live_doc] = client.docs.values()
        bogus = live_doc.model_copy(
            update={"doc_id": str(uuid.uuid4()), "artifact_id": uuid.uuid4()}
        )
        client.docs[bogus.doc_id] = bogus
    else:
        await session.execute(
            sa_update(KnowledgeArtifact)
            .where(KnowledgeArtifact.artifact_id == concept.artifact_id)
            .values(artifact_hash="ahash:changed-after-upsert")
        )

    validate = make_consistency_validator(client)
    with caplog.at_level(logging.ERROR, logger="kb_builder.indexer.consistency"):
        assert await validate(session, "v-build.1") is False

    assert any(f"event=index_drift class={drift_class}" in r.getMessage() for r in caplog.records)
