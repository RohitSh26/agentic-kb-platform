"""PostgresKeywordSearchClient against a real (local) Postgres registry.

The local/default relevance backend behind the SearchClient seam: title hits
weigh 2, body hits 1, results are scoped to the active build's interval
MEMBERSHIP (version-membership.md), and LIKE metacharacters in queries are
treated as literals.
"""

from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    insert_artifact,
    insert_build_run,
    require_registry_schema,
)
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.infrastructure.postgres.keyword_search import PostgresKeywordSearchClient

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


# Active build_seq for these tests; rows with valid_from_seq <= 5 and not
# invalidated at/below 5 are members of the served set.
ACTIVE_SEQ = 5


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await insert_build_run(session, KB_VERSION, "active", build_seq=ACTIVE_SEQ)
    yield


async def test_title_hits_outrank_body_hits(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        title_hit = await insert_artifact(
            session, title="Payment validation", body_text="nothing relevant"
        )
        body_hit = await insert_artifact(
            session, title="Checkout overview", body_text="covers payment flows"
        )
        await insert_artifact(session, title="Unrelated", body_text="graph traversal")
    client = PostgresKeywordSearchClient(factory)

    hits = await client.search("payment", build_seq=ACTIVE_SEQ, top=10)

    assert [hit.artifact_id for hit in hits] == [title_hit, body_hit]
    assert hits[0].score > hits[1].score


async def test_search_text_hit_retrieves_a_symbol_and_outranks_body(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # PR-34: a concept word the agent searches ("authentication") lives in a symbol's
    # search_text (split identifiers / docstring) but NOT its raw body — search_text must
    # surface it, and weight 1.5 ranks it above a row that only body-matches.
    async with factory() as session:
        via_search = await insert_artifact(
            session,
            title="validate_token",
            body_text="def validate_token(): ...",
            search_text="authentication token validate verify session",
        )
        via_body = await insert_artifact(
            session, title="notes", body_text="see the authentication doc"
        )
    client = PostgresKeywordSearchClient(factory)

    hits = await client.search("authentication", build_seq=ACTIVE_SEQ, top=10)

    ids = [hit.artifact_id for hit in hits]
    assert via_search in ids, "a search_text-only concept hit must be retrievable"
    assert ids[0] == via_search  # search_text (1.5) outranks a body-only (1.0) hit


async def test_distinctive_term_outranks_generic_overlap(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """IDF weighting: a rare, decisive token must beat broader overlap on common tokens.

    Without it, a "…graphify…code…graph…" question ranks generic retrieval/ranking cards
    over the graphify cards because every token scores equally. Here ``graphify`` hits only
    a TITLE (raw 2.0) while the off-topic card hits TWO common tokens in its title (raw 4.0);
    IDF lifts the rare ``graphify`` term above the common ``alpha``/``beta`` overlap.
    """
    async with factory() as session:
        on_topic = await insert_artifact(session, title="graphify", body_text="extraction backend")
        off_topic = await insert_artifact(
            session, title="alpha beta gadget", body_text="generic helper"
        )
        # decoys make alpha/beta common (high df -> low IDF); graphify stays rare (df=1)
        for i in range(8):
            await insert_artifact(session, title=f"misc {i}", body_text="alpha beta filler")
    client = PostgresKeywordSearchClient(factory)

    hits = await client.search("graphify alpha beta", build_seq=ACTIVE_SEQ, top=10)

    ids = [hit.artifact_id for hit in hits]
    assert ids[0] == on_topic, "the distinctive term must dominate generic overlap"
    assert ids.index(on_topic) < ids.index(off_topic)


async def test_scores_are_floats_so_the_ranker_can_weight_them(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Regression: Postgres NUMERIC arithmetic returns Decimal, but the ranker
    # multiplies the score by a float temporal weight — a Decimal score raises
    # "unsupported operand type(s) for *: 'Decimal' and 'float'" at create_pack time.
    async with factory() as session:
        await insert_artifact(session, title="Payment validation", body_text="payment flows")
    client = PostgresKeywordSearchClient(factory)

    hits = await client.search("payment", build_seq=ACTIVE_SEQ, top=10)

    assert hits
    assert all(isinstance(hit.score, float) for hit in hits)
    assert hits[0].score * 0.8 >= 0.0  # the multiplication the ranker performs


async def test_results_are_scoped_to_membership(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Membership, not label-equality: an artifact invalidated at or before the
    active build_seq is excluded; a still-live one introduced earlier is served."""
    async with factory() as session:
        # Invalidated at seq 3 (< active 5) ⇒ not a member of the served set.
        await insert_artifact(
            session,
            title="Payment validation",
            body_text="old",
            valid_from_seq=1,
            invalidated_at_seq=3,
        )
        # Introduced at seq 2, still live ⇒ a member even though it predates 5.
        current = await insert_artifact(
            session, title="Payment validation", body_text="new", valid_from_seq=2
        )
    client = PostgresKeywordSearchClient(factory)

    hits = await client.search("payment", build_seq=ACTIVE_SEQ, top=10)

    assert [hit.artifact_id for hit in hits] == [current]


async def test_like_metacharacters_are_matched_literally(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await insert_artifact(session, title="Percent rules", body_text="anything goes here")
    client = PostgresKeywordSearchClient(factory)

    # "%" normalizes away entirely; a query of only punctuation yields no tokens
    assert await client.search("%%%", build_seq=ACTIVE_SEQ, top=10) == []


async def test_empty_query_returns_no_hits(factory: async_sessionmaker[AsyncSession]) -> None:
    client = PostgresKeywordSearchClient(factory)
    assert await client.search("   ", build_seq=ACTIVE_SEQ, top=10) == []
