"""Broker serves by INTERVAL MEMBERSHIP, not kb_version label-equality (ADR-0013).

These are the mcp-server side of the PR-27 acceptance criteria
(docs/contracts/version-membership.md):

- an artifact introduced by an EARLIER build (valid_from_seq < active build_seq)
  but still live IS served by the active version — this is the incremental-serving
  bug fix; it would fail under the old `WHERE kb_version = :active` logic;
- an artifact INVALIDATED in the active build (invalidated_at_seq = active
  build_seq) is NOT served by that active version, but IS still served by the
  PRIOR version (invalidated_at_seq > prior build_seq).

The broker path under test is context.create_pack: search hints -> Postgres
hydration -> ACL, all membership-scoped against the resolved active build_seq.
Requires an externally migrated TEST_DATABASE_URL (kb-builder make migrate-test-db).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    clean_registry,
    insert_artifact,
    insert_build_run,
    make_broker_deps,
    require_registry_schema,
)
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.pack import create_pack
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp.tool_schemas.context import CreatePackRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

REQUESTER = Requester(subject="impl-agent", teams=frozenset())
RUN_ID = "run-membership"


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
    yield


def _pack_request() -> CreatePackRequest:
    return CreatePackRequest(
        run_id=RUN_ID,
        task="payment validation",
        approved_context_plan="review payment validation",
        retrieval_profile="default",
        budget_tokens=8000,
    )


async def _seed_searchable(
    session: AsyncSession,
    search: FakeSearchClient,
    *,
    title: str,
    valid_from_seq: int,
    invalidated_at_seq: int | None = None,
) -> uuid.UUID:
    artifact_id = await insert_artifact(
        session,
        title=title,
        body_text="payment validation rules",
        valid_from_seq=valid_from_seq,
        invalidated_at_seq=invalidated_at_seq,
    )
    search.seed("payment", [SearchHit(artifact_id=artifact_id, score=2.0)])
    return artifact_id


async def test_incremental_serving_regression_serves_all_live_artifacts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE bug fix: v1 introduces 3 sources; v2 changes only 1 (2 carried by cache
    hit at their original valid_from_seq=1). Activating v2 (build_seq=2) must serve
    ALL 3 — not just the one re-created at seq 2. Under the old `= kb_version`
    logic only the seq-2 row would be served, so this asserts the regression is
    fixed."""
    search = FakeSearchClient()
    async with factory() as session:
        # build_seq=2 is active (v2). v1 was build_seq=1.
        await insert_build_run(session, "kb-v1", "superseded", build_seq=1)
        await insert_build_run(session, "kb-v2", "active", build_seq=2)
        carried_a = await insert_artifact(
            session, title="alpha", body_text="payment alpha", valid_from_seq=1
        )
        carried_b = await insert_artifact(
            session, title="beta", body_text="payment beta", valid_from_seq=1
        )
        changed = await insert_artifact(
            session, title="gamma", body_text="payment gamma", valid_from_seq=2
        )
        search.seed(
            "payment",
            [
                SearchHit(artifact_id=carried_a, score=3.0),
                SearchHit(artifact_id=carried_b, score=2.0),
                SearchHit(artifact_id=changed, score=1.0),
            ],
        )
    deps = make_broker_deps(factory, search)

    response = await create_pack(deps, _pack_request(), REQUESTER)

    served = {card.artifact_id for card in response.evidence_cards}
    assert served == {carried_a, carried_b, changed}, (
        "active version must serve every LIVE artifact (members), not only the day's changed delta"
    )


async def test_earlier_introduced_artifact_is_served_by_later_version(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await insert_build_run(session, "kb-v3", "active", build_seq=3)
        # introduced at seq 1, still live ⇒ a member of version 3.
        earlier = await _seed_searchable(session, search, title="early", valid_from_seq=1)
    deps = make_broker_deps(factory, search)

    response = await create_pack(deps, _pack_request(), REQUESTER)

    assert [card.artifact_id for card in response.evidence_cards] == [earlier]


async def test_invalidated_artifact_hidden_by_invalidating_version_but_served_by_prior(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An artifact invalidated in build 2 is NOT a member of version 2, but IS a
    member of the prior version 1 (immutability: invalidating never removes it
    from versions < the invalidating seq)."""
    search = FakeSearchClient()
    async with factory() as session:
        artifact_id = await insert_artifact(
            session,
            title="legacy",
            body_text="payment legacy",
            valid_from_seq=1,
            invalidated_at_seq=2,
        )
        search.seed("payment", [SearchHit(artifact_id=artifact_id, score=2.0)])

    # Active = the invalidating version 2: the artifact must NOT be served.
    async with factory() as session:
        await insert_build_run(session, "kb-v2", "active", build_seq=2)
    deps = make_broker_deps(factory, search)
    response_v2 = await create_pack(deps, _pack_request(), REQUESTER)
    assert artifact_id not in {c.artifact_id for c in response_v2.evidence_cards}
    assert response_v2.open_questions  # nothing served ⇒ open question

    # Active = the prior version 1: the artifact is STILL served (immutability).
    async with factory() as session:
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
        await insert_build_run(session, "kb-v1", "active", build_seq=1)
    response_v1 = await create_pack(deps, _pack_request(), REQUESTER)
    assert [c.artifact_id for c in response_v1.evidence_cards] == [artifact_id]
