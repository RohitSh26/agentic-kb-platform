"""PR-33 temporal-semantics retrieval ranking against a real (local) Postgres.

Asserts the broker reorders evidence by query intent — current code first for
`how_does_x_work`, cards/PRs included for `why_was_x_changed` — that a doc
referencing a removed symbol is downranked and never primary for `how`, that the
ordering is deterministic and logged, and that the PR-33 stale signal stays
INDEPENDENT of the verifier's L0 not_stale check (a PR-33-stale doc still passes
L0 because its source is in-version). Requires an externally migrated
TEST_DATABASE_URL (kb-builder `make migrate-test-db`); skips otherwise.
"""

import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    insert_artifact,
    insert_build_run,
    make_broker_deps,
    require_registry_schema,
)
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.retrieval import retrieve_cards
from agentic_mcp_server.context_broker.verify import verify_answer
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp.tool_schemas.verification import ClaimInput, VerifyAnswerRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())


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


async def _seed_code_and_doc(
    session: AsyncSession, search: FakeSearchClient
) -> tuple[uuid.UUID, uuid.UUID]:
    """A current code symbol + an equally-relevant doc, both matching `topic`."""
    code_id = await insert_artifact(
        session,
        title="helper",
        body_text="def helper(): ...",
        artifact_type="code_symbol",
        source_type="github_code",
    )
    doc_id = await insert_artifact(
        session,
        title="Helper guide",
        body_text="The `helper` function explains the topic.",
        artifact_type="doc_chunk",
        source_type="github_doc",
    )
    # equal search scores so the temporal weight, not relevance, decides order
    search.seed(
        "topic",
        [SearchHit(artifact_id=code_id, score=1.0), SearchHit(artifact_id=doc_id, score=1.0)],
    )
    return code_id, doc_id


async def test_how_intent_ranks_current_code_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        code_id, doc_id = await _seed_code_and_doc(session, search)
    deps = make_broker_deps(factory, search)

    cards, _ = await retrieve_cards(
        deps,
        query="topic",
        kb_version=KB_VERSION,
        build_seq=1,
        requester=REQUESTER,
        tool="context.create_pack",
        intent="how_does_x_work",
    )
    assert cards[0].artifact_id == code_id
    assert cards[0].source_kind == "code"
    assert {c.artifact_id for c in cards} == {code_id, doc_id}  # nothing dropped


async def test_why_intent_includes_and_lifts_card(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        code_id = await insert_artifact(
            session,
            title="service_fn",
            body_text="def service_fn(): ...",
            artifact_type="code_symbol",
            source_type="github_code",
        )
        card_id = await insert_artifact(
            session,
            title="Work item 4321",
            body_text="Changed service to fix a billing bug.",
            artifact_type="card",
            source_type="ado_card",
        )
        search.seed(
            "topic",
            [
                SearchHit(artifact_id=code_id, score=1.0),
                SearchHit(artifact_id=card_id, score=1.0),
            ],
        )
    deps = make_broker_deps(factory, search)

    cards, _ = await retrieve_cards(
        deps,
        query="topic",
        kb_version=KB_VERSION,
        build_seq=1,
        requester=REQUESTER,
        tool="context.create_pack",
        intent="why_was_x_changed",
    )
    ids = [c.artifact_id for c in cards]
    assert card_id in ids  # the card is included for "why"
    assert ids[0] == card_id  # and lifted above current code
    assert next(c for c in cards if c.artifact_id == card_id).source_kind == "card"


async def test_stale_doc_not_primary_for_how(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        code_id = await insert_artifact(
            session,
            title="helper",
            body_text="def helper(): ...",
            artifact_type="code_symbol",
            source_type="github_code",
        )
        # doc references a symbol that is NOT a current code member -> PR-33 stale
        stale_doc_id = await insert_artifact(
            session,
            title="Legacy guide",
            body_text="Use `removed_legacy_symbol` to do the thing.",
            artifact_type="doc_chunk",
            source_type="github_doc",
        )
        # give the stale doc a HIGHER raw search score; temporal must still demote it
        search.seed(
            "topic",
            [
                SearchHit(artifact_id=stale_doc_id, score=5.0),
                SearchHit(artifact_id=code_id, score=1.0),
            ],
        )
    deps = make_broker_deps(factory, search)

    cards, _ = await retrieve_cards(
        deps,
        query="topic",
        kb_version=KB_VERSION,
        build_seq=1,
        requester=REQUESTER,
        tool="context.create_pack",
        intent="how_does_x_work",
    )
    assert cards[0].artifact_id == code_id  # current code is primary
    stale_card = next(c for c in cards if c.artifact_id == stale_doc_id)
    assert stale_card.stale_for_intent is True
    assert cards[0].artifact_id != stale_doc_id  # stale doc never primary
    assert stale_doc_id in {c.artifact_id for c in cards}  # surfaced as a hint, not removed


async def test_ranking_is_deterministic(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_code_and_doc(session, search)
    deps = make_broker_deps(factory, search)

    async def order() -> list[uuid.UUID]:
        cards, _ = await retrieve_cards(
            deps,
            query="topic",
            kb_version=KB_VERSION,
            build_seq=1,
            requester=REQUESTER,
            tool="context.create_pack",
            intent="how_does_x_work",
        )
        return [c.artifact_id for c in cards]

    assert await order() == await order()


async def test_weighting_is_logged(
    factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_code_and_doc(session, search)
    deps = make_broker_deps(factory, search)

    with caplog.at_level(logging.INFO, logger="agentic_mcp_server.context_broker"):
        await retrieve_cards(
            deps,
            query="topic",
            kb_version=KB_VERSION,
            build_seq=1,
            requester=REQUESTER,
            tool="context.create_pack",
            intent="how_does_x_work",
        )
    messages = [r.getMessage() for r in caplog.records]
    assert any("event=temporal_weight " in m for m in messages)
    assert any("event=temporal_weight_summary" in m for m in messages)


async def test_pr33_stale_doc_still_passes_l0_not_stale(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The two 'stale' notions are independent: a doc PR-33 downranks for `how`
    still PASSES the verifier's L0 not_stale check, because its SOURCE is in-version
    (not is_deleted). PR-33 never touches verify.py's L0 logic."""
    search = FakeSearchClient()
    async with factory() as session:
        stale_doc_id = await insert_artifact(
            session,
            title="Legacy guide",
            body_text="Use `removed_legacy_symbol`.",
            artifact_type="doc_chunk",
            source_type="github_doc",
            source_is_deleted=False,  # source IS in-version -> L0 not_stale must pass
        )
        # record the requester ledger row so L0 in_requester_ledger passes
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id="run-temporal",
                agent_name=SUBJECT,
                tool_name="context.create_pack",
                status="approved",
                kb_version=KB_VERSION,
                returned_artifact_ids=[stale_doc_id],
                new_evidence_ids=[stale_doc_id],
            ),
        )

    # confirm PR-33 flags it stale for `how`
    deps = make_broker_deps(factory, search)
    search.seed("topic", [SearchHit(artifact_id=stale_doc_id, score=1.0)])
    cards, _ = await retrieve_cards(
        deps,
        query="topic",
        kb_version=KB_VERSION,
        build_seq=1,
        requester=REQUESTER,
        tool="context.create_pack",
        intent="how_does_x_work",
    )
    assert next(c for c in cards if c.artifact_id == stale_doc_id).stale_for_intent is True

    # ...and the SAME doc passes L0 not_stale in the verifier (independent notions)
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="ans-temporal",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="The legacy guide explains the symbol.",
                    evidence_ids=[str(stale_doc_id)],
                )
            ],
            verifier_levels=["L0"],
        ),
        REQUESTER,
    )
    claim = receipt.claim_results[0]
    assert claim.checks.L0_not_stale is True
