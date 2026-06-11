"""PostgresKeywordSearchClient against a real (local) Postgres registry.

The local/default relevance backend behind the SearchClient seam: title hits
weigh 2, body hits 1, results are scoped to one kb_version, and LIKE
metacharacters in queries are treated as literals.
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


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await insert_build_run(session, KB_VERSION, "active")
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

    hits = await client.search("payment", kb_version=KB_VERSION, top=10)

    assert [hit.artifact_id for hit in hits] == [title_hit, body_hit]
    assert hits[0].score > hits[1].score


async def test_results_are_scoped_to_the_requested_kb_version(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await insert_artifact(
            session, title="Payment validation", body_text="old", kb_version="kb-old"
        )
        current = await insert_artifact(session, title="Payment validation", body_text="new")
    client = PostgresKeywordSearchClient(factory)

    hits = await client.search("payment", kb_version=KB_VERSION, top=10)

    assert [hit.artifact_id for hit in hits] == [current]


async def test_like_metacharacters_are_matched_literally(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await insert_artifact(session, title="Percent rules", body_text="anything goes here")
    client = PostgresKeywordSearchClient(factory)

    # "%" normalizes away entirely; a query of only punctuation yields no tokens
    assert await client.search("%%%", kb_version=KB_VERSION, top=10) == []


async def test_empty_query_returns_no_hits(factory: async_sessionmaker[AsyncSession]) -> None:
    client = PostgresKeywordSearchClient(factory)
    assert await client.search("   ", kb_version=KB_VERSION, top=10) == []
