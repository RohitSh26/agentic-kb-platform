"""Linker tests (PR-07 acceptance criteria).

Pure tests cover deterministic matching, precision guards, and the semantic
fallback. DB-backed tests cover the canonical example chain, rerun
idempotency, the unchanged-set skip, and low-confidence flagging; they are
skipped gracefully when TEST_DATABASE_URL is not configured, same policy as
packages/db tests. The similarity provider is a fake — no Azure calls.
"""

import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import KnowledgeArtifact, KnowledgeEdge, SourceItem
from kb_builder.linker import (
    DOC_LINK_CONFIDENCE,
    EDGE_SOURCE,
    IMPLEMENTS_CONFIDENCE,
    LinkableArtifact,
    ScoredArtifact,
    find_deterministic_links,
    find_semantic_links,
    run_linker,
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

SYMBOL_TITLE = "EmbeddingService.get_user_embedding"
ENDPOINT_TITLE = "GET /users/{userId}/embeddings"
CODE_FILE_TITLE = "src/embeddings/service.py"


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    assert TEST_DATABASE_URL is not None
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    # alembic's fileConfig() disables already-created loggers; re-enable the
    # linker loggers so caplog assertions on structured events still work.
    for name in ("kb_builder.linker.run", "kb_builder.linker.write_edges"):
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


def _linkable(
    artifact_type: str,
    title: str | None,
    body_text: str | None,
    source_type: str,
) -> LinkableArtifact:
    return LinkableArtifact(
        artifact_id=uuid.uuid4(),
        artifact_type=artifact_type,
        title=title,
        body_text=body_text,
        source_type=source_type,
    )


class FakeSimilarityProvider:
    """Canned similar_code_symbols results keyed by querying artifact_id."""

    def __init__(self, results: dict[uuid.UUID, list[ScoredArtifact]]) -> None:
        self._results = results
        self.calls: list[uuid.UUID] = []

    async def similar_code_symbols(
        self, *, artifact_id: uuid.UUID, top_k: int
    ) -> Sequence[ScoredArtifact]:
        self.calls.append(artifact_id)
        return self._results.get(artifact_id, [])


# ---------------------------------------------------------------------------
# Pure deterministic matching
# ---------------------------------------------------------------------------


def test_deterministic_links_canonical_fixture() -> None:
    concept = _linkable(
        "concept",
        "User Embeddings",
        f"Per-user vectors computed by {SYMBOL_TITLE}.",
        "azure_wiki",
    )
    symbol = _linkable(
        "code_symbol", SYMBOL_TITLE, "def get_user_embedding(...): ...", "github_code"
    )
    wiki_doc = _linkable(
        "summary",
        "Embeddings wiki page",
        "This page explains how user embeddings are refreshed nightly.",  # case differs
        "azure_wiki",
    )
    card_doc = _linkable(
        "summary",
        "ADO card 1234",
        "Feature request: ship User Embeddings for personalization.",
        "ado_card",
    )
    code_file = _linkable("code_file", CODE_FILE_TITLE, None, "github_code")
    chunk = _linkable(
        "chunk",
        "readme chunk",
        f"The embedding pipeline lives in {CODE_FILE_TITLE} today.",
        "github_doc",
    )

    drafts = find_deterministic_links([concept, symbol, wiki_doc, card_doc, code_file, chunk])

    edges = {(d.from_artifact_id, d.to_artifact_id, d.edge_type, d.confidence) for d in drafts}
    assert edges == {
        (symbol.artifact_id, concept.artifact_id, "implements", IMPLEMENTS_CONFIDENCE),
        (wiki_doc.artifact_id, concept.artifact_id, "documents", DOC_LINK_CONFIDENCE),
        (card_doc.artifact_id, concept.artifact_id, "requests", DOC_LINK_CONFIDENCE),
        (chunk.artifact_id, code_file.artifact_id, "mentions", DOC_LINK_CONFIDENCE),
    }
    assert all(d.strategy == "deterministic" for d in drafts)


PRECISION_GUARD_CASES: list[tuple[str, list[LinkableArtifact]]] = [
    (
        "short_single_word_concept_title_is_ignored",
        [
            _linkable("concept", "User", "The User concept.", "azure_wiki"),
            _linkable("summary", "wiki page", "Every User gets a profile.", "azure_wiki"),
            _linkable("summary", "ado card", "User stories mention User a lot.", "ado_card"),
        ],
    ),
    (
        "short_symbol_title_is_ignored",
        [
            _linkable("code_symbol", "f", "def f(): ...", "github_code"),
            _linkable("concept", "Helper Functions", "Uses f everywhere.", "azure_wiki"),
            _linkable("summary", "wiki page", "Call f to do the thing.", "azure_wiki"),
        ],
    ),
    (
        "symbol_never_matches_inside_longer_identifier",
        [
            _linkable("code_symbol", "get_user", "def get_user(): ...", "github_code"),
            _linkable(
                "concept",
                "User Embeddings",
                "Computed by get_user_embedding only.",
                "azure_wiki",
            ),
            _linkable(
                "summary",
                "wiki page",
                f"Documentation for {SYMBOL_TITLE} internals.",
                "azure_wiki",
            ),
        ],
    ),
]


@pytest.mark.parametrize(
    "artifacts",
    [case for _, case in PRECISION_GUARD_CASES],
    ids=[name for name, _ in PRECISION_GUARD_CASES],
)
def test_deterministic_precision_guards(artifacts: list[LinkableArtifact]) -> None:
    assert find_deterministic_links(artifacts) == []


# ---------------------------------------------------------------------------
# Pure semantic fallback
# ---------------------------------------------------------------------------


async def test_semantic_fallback_threshold_and_existing_pair_skip() -> None:
    concept = _linkable("concept", "Vector Search Ranking", "Ranks results.", "azure_wiki")
    symbol_id = uuid.uuid4()
    other_id = uuid.uuid4()
    provider = FakeSimilarityProvider(
        {
            concept.artifact_id: [
                ScoredArtifact(artifact_id=symbol_id, similarity=0.93),
                ScoredArtifact(artifact_id=other_id, similarity=0.50),
            ]
        }
    )

    drafts = await find_semantic_links(provider, [concept], existing_pairs=set())

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.from_artifact_id == symbol_id
    assert draft.to_artifact_id == concept.artifact_id
    assert draft.edge_type == "implements"
    assert draft.confidence == pytest.approx(0.93)
    assert draft.strategy == "semantic"
    # the 0.50 candidate is rejected below SEMANTIC_ACCEPT_THRESHOLD
    assert all(d.from_artifact_id != other_id for d in drafts)

    # deterministic wins: a pair already present is never re-proposed
    existing = {(symbol_id, concept.artifact_id, "implements")}
    assert await find_semantic_links(provider, [concept], existing_pairs=existing) == []


# ---------------------------------------------------------------------------
# DB-backed: canonical chain, idempotency, unchanged-set skip
# ---------------------------------------------------------------------------


async def _add_source(session: AsyncSession, source_type: str, source_uri: str) -> SourceItem:
    source = SourceItem(
        source_type=source_type,
        source_uri=source_uri,
        source_version="1",
        content_hash=f"hash:{source_uri}",
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
    kb_version: str = "v-build.1",
) -> KnowledgeArtifact:
    artifact = KnowledgeArtifact(
        artifact_type=artifact_type,
        source_id=source.source_id,
        title=title,
        body_text=body_text,
        kb_version=kb_version,
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def _seed_canonical_chain(session: AsyncSession) -> dict[str, KnowledgeArtifact]:
    """Concept + wiki summary + ADO summary + symbol + endpoint + test, plus graphify edges."""
    wiki = await _add_source(session, "azure_wiki", "wiki://embeddings-page")
    card = await _add_source(session, "ado_card", "ado://card/1234")
    code = await _add_source(session, "github_code", f"github://org/repo/{CODE_FILE_TITLE}")

    artifacts = {
        "concept": await _add_artifact(
            session,
            source=wiki,
            artifact_type="concept",
            title="User Embeddings",
            body_text=f"Per-user vectors computed by {SYMBOL_TITLE}.",
        ),
        "wiki_summary": await _add_artifact(
            session,
            source=wiki,
            artifact_type="summary",
            title="Embeddings wiki page",
            body_text="This page explains how user embeddings are refreshed nightly.",
        ),
        "card_summary": await _add_artifact(
            session,
            source=card,
            artifact_type="summary",
            title="ADO card 1234",
            body_text="Feature request: ship User Embeddings for personalization.",
        ),
        "symbol": await _add_artifact(
            session,
            source=code,
            artifact_type="code_symbol",
            title=SYMBOL_TITLE,
            body_text="def get_user_embedding(self, user_id: str) -> list[float]: ...",
        ),
        "endpoint": await _add_artifact(
            session,
            source=code,
            artifact_type="endpoint",
            title=ENDPOINT_TITLE,
            body_text=None,
        ),
        "test": await _add_artifact(
            session,
            source=code,
            artifact_type="test",
            title="test_get_user_embedding_returns_vector",
            body_text=None,
        ),
    }
    session.add_all(
        [
            KnowledgeEdge(
                from_artifact_id=artifacts["symbol"].artifact_id,
                to_artifact_id=artifacts["endpoint"].artifact_id,
                edge_type="exposed_as",
                confidence=1.0,
                source="graphify",
                kb_version="v-build.1",
            ),
            KnowledgeEdge(
                from_artifact_id=artifacts["test"].artifact_id,
                to_artifact_id=artifacts["symbol"].artifact_id,
                edge_type="tests",
                confidence=1.0,
                source="graphify",
                kb_version="v-build.1",
            ),
        ]
    )
    await session.flush()
    return artifacts


async def _edge_tuples(
    session: AsyncSession, *, source: str | None = None
) -> set[tuple[uuid.UUID, uuid.UUID, str, str | None, str]]:
    query = select(
        KnowledgeEdge.from_artifact_id,
        KnowledgeEdge.to_artifact_id,
        KnowledgeEdge.edge_type,
        KnowledgeEdge.source,
        KnowledgeEdge.kb_version,
    )
    if source is not None:
        query = query.where(KnowledgeEdge.source == source)
    rows = await session.execute(query)
    return set(rows.tuples())


async def _linker_edge_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeEdge)
            .where(KnowledgeEdge.source == EDGE_SOURCE)
        )
    ).scalar_one()


@requires_db
async def test_run_linker_completes_canonical_chain(session: AsyncSession) -> None:
    artifacts = await _seed_canonical_chain(session)

    inserted, refreshed, deleted = await run_linker(session, kb_version="v-link.1")
    assert (inserted, refreshed, deleted) == (3, 0, 0)

    edges = await _edge_tuples(session)
    concept = artifacts["concept"].artifact_id
    symbol = artifacts["symbol"].artifact_id
    expected = {
        # linker edges, source='linker'
        (artifacts["wiki_summary"].artifact_id, concept, "documents", "linker", "v-link.1"),
        (artifacts["card_summary"].artifact_id, concept, "requests", "linker", "v-link.1"),
        (symbol, concept, "implements", "linker", "v-link.1"),
        # pre-existing graphify edges complete the chain through endpoint and test
        (symbol, artifacts["endpoint"].artifact_id, "exposed_as", "graphify", "v-build.1"),
        (artifacts["test"].artifact_id, symbol, "tests", "graphify", "v-build.1"),
    }
    assert edges == expected

    # every linker edge stores a confidence
    rows = await session.execute(
        select(KnowledgeEdge.edge_type, KnowledgeEdge.confidence).where(
            KnowledgeEdge.source == EDGE_SOURCE
        )
    )
    confidences = {edge_type: confidence for edge_type, confidence in rows.tuples()}
    assert confidences == {
        "documents": pytest.approx(DOC_LINK_CONFIDENCE),
        "requests": pytest.approx(DOC_LINK_CONFIDENCE),
        "implements": pytest.approx(IMPLEMENTS_CONFIDENCE),
    }

    # the concept is now fully traversable to wiki, card, symbol, endpoint, and test
    adjacency = {a for f, t, *_ in edges for a in (f, t)}
    assert adjacency == {a.artifact_id for a in artifacts.values()}


@requires_db
async def test_run_linker_rerun_same_version_is_idempotent(session: AsyncSession) -> None:
    artifacts = await _seed_canonical_chain(session)

    first = await run_linker(session, kb_version="v-link.1")
    assert first == (3, 0, 0)
    count_after_first = await _linker_edge_count(session)

    # a retuned confidence between retries is observably refreshed in place
    await session.execute(
        sa_update(KnowledgeEdge)
        .where(
            KnowledgeEdge.source == EDGE_SOURCE,
            KnowledgeEdge.from_artifact_id == artifacts["symbol"].artifact_id,
        )
        .values(confidence=0.5)
    )

    inserted, refreshed, deleted = await run_linker(session, kb_version="v-link.1")
    assert (inserted, refreshed, deleted) == (0, 3, 0)  # never inserts duplicates
    assert await _linker_edge_count(session) == count_after_first == 3
    restored = (
        await session.execute(
            select(KnowledgeEdge.confidence).where(
                KnowledgeEdge.source == EDGE_SOURCE,
                KnowledgeEdge.from_artifact_id == artifacts["symbol"].artifact_id,
            )
        )
    ).scalar_one()
    assert restored == pytest.approx(IMPLEMENTS_CONFIDENCE)


@requires_db
async def test_rerun_under_new_version_refreshes_in_place(session: AsyncSession) -> None:
    await _seed_canonical_chain(session)
    await run_linker(session, kb_version="v-link.1")

    result = await run_linker(session, kb_version="v-link.2")

    assert result == (0, 3, 0)  # one row per logical link — no copy per version
    edges = await _edge_tuples(session, source=EDGE_SOURCE)
    assert len(edges) == 3
    assert all(kb_version == "v-link.2" for *_, kb_version in edges)


@requires_db
async def test_run_linker_writes_new_version_when_set_changes(session: AsyncSession) -> None:
    artifacts = await _seed_canonical_chain(session)
    await run_linker(session, kb_version="v-link.1")

    payments = await _add_source(session, "azure_wiki", "wiki://payment-routing")
    new_concept = await _add_artifact(
        session,
        source=payments,
        artifact_type="concept",
        title="Payment Routing",
        body_text="Implemented by PaymentRouter.route_payment in the billing service.",
    )
    code = await _add_source(session, "github_code", "github://org/repo/src/billing/router.py")
    new_symbol = await _add_artifact(
        session,
        source=code,
        artifact_type="code_symbol",
        title="PaymentRouter.route_payment",
        body_text="def route_payment(self): ...",
    )

    inserted, refreshed, deleted = await run_linker(session, kb_version="v-link.3")
    assert (inserted, refreshed, deleted) == (1, 3, 0)

    edges = await _edge_tuples(session, source=EDGE_SOURCE)
    assert len(edges) == 4
    assert (
        new_symbol.artifact_id,
        new_concept.artifact_id,
        "implements",
        "linker",
        "v-link.3",
    ) in edges
    # the original logical edge still exists exactly once, refreshed in place
    assert (
        artifacts["symbol"].artifact_id,
        artifacts["concept"].artifact_id,
        "implements",
        "linker",
        "v-link.3",
    ) in edges


@requires_db
async def test_stale_edge_is_deleted_when_evidence_disappears(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts = await _seed_canonical_chain(session)
    await run_linker(session, kb_version="v-link.1")
    assert await _linker_edge_count(session) == 3

    # the ADO card no longer mentions the concept: its requests edge is stale
    await session.execute(
        sa_update(KnowledgeArtifact)
        .where(KnowledgeArtifact.artifact_id == artifacts["card_summary"].artifact_id)
        .values(body_text="Feature request: ship personalization improvements.")
    )

    with caplog.at_level(logging.INFO, logger="kb_builder.linker.write_edges"):
        inserted, refreshed, deleted = await run_linker(session, kb_version="v-link.2")

    assert (inserted, refreshed, deleted) == (0, 2, 1)
    assert any("event=linker_edge_deleted" in r.getMessage() for r in caplog.records)
    edges = await _edge_tuples(session, source=EDGE_SOURCE)
    assert {edge_type for _, _, edge_type, *_ in edges} == {"documents", "implements"}


@requires_db
async def test_run_linker_semantic_edge_is_flagged_low_confidence(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    wiki = await _add_source(session, "azure_wiki", "wiki://ranking")
    concept = await _add_artifact(
        session,
        source=wiki,
        artifact_type="concept",
        title="Vector Search Ranking",
        body_text="How results are ordered before evidence packing.",
    )
    code = await _add_source(session, "github_code", "github://org/repo/src/search/ranker.py")
    symbol = await _add_artifact(
        session,
        source=code,
        artifact_type="code_symbol",
        title="Ranker.score_documents",
        body_text="def score_documents(self): ...",
    )
    provider = FakeSimilarityProvider(
        {concept.artifact_id: [ScoredArtifact(artifact_id=symbol.artifact_id, similarity=0.85)]}
    )

    with caplog.at_level(logging.WARNING, logger="kb_builder.linker.write_edges"):
        inserted, refreshed, deleted = await run_linker(
            session, kb_version="v-sem.1", similarity=provider
        )

    assert (inserted, refreshed, deleted) == (1, 0, 0)
    assert provider.calls == [concept.artifact_id]  # no deterministic match, fallback used
    row = (
        await session.execute(select(KnowledgeEdge).where(KnowledgeEdge.source == EDGE_SOURCE))
    ).scalar_one()
    assert row.from_artifact_id == symbol.artifact_id
    assert row.to_artifact_id == concept.artifact_id
    assert row.edge_type == "implements"
    assert row.confidence == pytest.approx(0.85)
    flagged = [r for r in caplog.records if "event=linker_low_confidence_edge" in r.getMessage()]
    assert len(flagged) == 1
    assert flagged[0].levelno == logging.WARNING


@requires_db
async def test_semantic_edge_survives_rerun_without_provider(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A degraded nightly (similarity=None) must not delete semantic implements edges."""
    wiki = await _add_source(session, "azure_wiki", "wiki://ranking")
    concept = await _add_artifact(
        session,
        source=wiki,
        artifact_type="concept",
        title="Vector Search Ranking",
        body_text="How results are ordered before evidence packing.",
    )
    chunk = await _add_artifact(
        session,
        source=wiki,
        artifact_type="chunk",
        title="Ranking wiki section",
        body_text="Vector Search Ranking decides result order.",
    )
    code = await _add_source(session, "github_code", "github://org/repo/src/search/ranker.py")
    symbol = await _add_artifact(
        session,
        source=code,
        artifact_type="code_symbol",
        title="Ranker.score_documents",
        body_text="def score_documents(self): ...",
    )
    provider = FakeSimilarityProvider(
        {concept.artifact_id: [ScoredArtifact(artifact_id=symbol.artifact_id, similarity=0.85)]}
    )
    inserted, refreshed, deleted = await run_linker(
        session, kb_version="v-sem.1", similarity=provider
    )
    assert (inserted, refreshed, deleted) == (2, 0, 0)  # semantic implements + documents

    # the chunk no longer mentions the concept, so its documents edge is stale
    await session.execute(
        sa_update(KnowledgeArtifact)
        .where(KnowledgeArtifact.artifact_id == chunk.artifact_id)
        .values(body_text="This section was rewritten and covers something else.")
    )

    with caplog.at_level(logging.INFO, logger="kb_builder.linker.write_edges"):
        inserted, refreshed, deleted = await run_linker(session, kb_version="v-sem.2")

    # documents edge deleted (evidence gone); semantic implements edge protected
    assert (inserted, refreshed, deleted) == (0, 0, 1)
    assert any(
        "event=linker_stale_deletion_skipped" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )
    edges = await _edge_tuples(session, source=EDGE_SOURCE)
    assert edges == {
        (symbol.artifact_id, concept.artifact_id, "implements", EDGE_SOURCE, "v-sem.1")
    }
