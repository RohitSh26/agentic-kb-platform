"""kb_search behavior against a real (local) Postgres registry (PR-37, ADR-0025).

Covers the acceptance criteria end to end: budget enforced server-side on BOTH
axes independently (call count and token cap), a retrieval_event row written
per call (approved, denied, and error), ACL filtering on returned results, and
budget_remaining stated on every response. Relevance/ranking itself is
PostgresKeywordSearchClient's surface, covered by test_keyword_search.py.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    RaisingSearchClient,
    clean_registry,
    fetch_ledger_rows,
    insert_artifact,
    insert_build_run,
    make_broker_deps,
    require_registry_schema,
)
from fastmcp.exceptions import ToolError
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.budgets import AgentAllowance, BudgetPolicy
from agentic_mcp_server.context_broker.constants import NO_RUN_SENTINEL
from agentic_mcp_server.context_broker.kb_search import BUDGET_SPENT_NOTICE, kb_search
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp.tool_schemas.search import KbSearchRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())
SESSION = "mcp-session-1"


def _policy(max_requests: int, max_tokens: int) -> BudgetPolicy:
    return BudgetPolicy(
        allowances={SUBJECT: AgentAllowance(max_requests=max_requests, max_tokens=max_tokens)}
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


async def _seed_payment_artifact(
    session: AsyncSession,
    search: FakeSearchClient,
    *,
    title: str = "Payment validation rules",
) -> uuid.UUID:
    artifact_id = await insert_artifact(
        session,
        title=title,
        body_text="Validation lives in checkout/validators.py and rejects negative amounts.",
        source_uri=f"github://org/repo/checkout/validators-{uuid.uuid4().hex[:6]}.py",
    )
    search.seed("payment", [SearchHit(artifact_id=artifact_id, score=2.0)])
    return artifact_id


async def test_search_returns_shaped_hits_and_writes_an_approved_ledger_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        artifact_id = await _seed_payment_artifact(session, search)
    deps = make_broker_deps(factory, search, budget_policy=_policy(4, 3000))

    response = await kb_search(
        deps, KbSearchRequest(query="payment validation"), REQUESTER, session_key=SESSION
    )

    assert len(response.results) == 1
    hit = response.results[0]
    assert hit.title == "Payment validation rules"
    assert hit.artifact_type == "doc_chunk"
    assert hit.source_uri is not None and hit.source_uri.startswith("github://")
    assert "checkout/validators.py" in hit.snippet
    # keyword hits are relevance-ranked, not cross-validated: always `interpreted`
    assert hit.confidence_tier == "interpreted"
    assert response.notice is None
    # the response states the remaining budget on both axes
    assert response.budget_remaining.calls == 3
    assert 0 < response.budget_remaining.tokens < 3000

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in rows] == [("kb_search", "approved")]
    assert rows[0].agent_name == SUBJECT
    assert rows[0].tokens_returned == 3000 - response.budget_remaining.tokens
    assert list(rows[0].new_evidence_ids or []) == []
    async with factory() as session:
        returned = (
            await session.execute(
                text("SELECT returned_artifact_ids, query_text FROM retrieval_event")
            )
        ).one()
    assert list(returned.returned_artifact_ids) == [artifact_id]
    assert returned.query_text == "payment validation"


async def test_call_cap_blocks_the_next_call_even_with_tokens_left(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_payment_artifact(session, search)
    deps = make_broker_deps(factory, search, budget_policy=_policy(1, 100_000))
    request = KbSearchRequest(query="payment validation")

    first = await kb_search(deps, request, REQUESTER, session_key=SESSION)
    assert first.results
    assert first.budget_remaining.calls == 0
    assert first.budget_remaining.tokens > 0  # tokens alone would still allow more
    # the closing call already tells the agent to move on (kb_agent.py parity)
    assert first.notice == BUDGET_SPENT_NOTICE

    second = await kb_search(deps, request, REQUESTER, session_key=SESSION)
    assert second.results == []
    assert second.notice == BUDGET_SPENT_NOTICE
    assert second.budget_remaining.calls == 0

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    # a retrieval_event row per call — the refusal is audited too
    assert [(row.tool_name, row.status) for row in rows] == [
        ("kb_search", "approved"),
        ("kb_search", "denied"),
    ]
    assert rows[1].tokens_returned == 0


async def test_token_cap_blocks_the_next_call_even_with_calls_left(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_payment_artifact(session, search)
    deps = make_broker_deps(factory, search, budget_policy=_policy(10, 1))
    request = KbSearchRequest(query="payment validation")

    # charged after the answer: the first call succeeds and overdraws the 1-token cap
    first = await kb_search(deps, request, REQUESTER, session_key=SESSION)
    assert first.results
    assert first.budget_remaining.tokens == 0  # floored, overdraft never reads negative
    assert first.budget_remaining.calls == 9  # calls alone would still allow more
    assert first.notice == BUDGET_SPENT_NOTICE

    second = await kb_search(deps, request, REQUESTER, session_key=SESSION)
    assert second.results == []
    assert second.notice == BUDGET_SPENT_NOTICE
    assert second.budget_remaining.calls == 9

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [row.status for row in rows] == ["approved", "denied"]


async def test_acl_filters_hits_by_requester_teams(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        public_id = await insert_artifact(
            session,
            title="Public payment doc",
            body_text="Validation lives in checkout/validators.py and rejects negative amounts.",
        )
        restricted_id = await insert_artifact(
            session,
            title="Restricted payment runbook",
            body_text="Secret payment escalation path.",
            acl_teams=["payments-team"],
        )
    search.seed(
        "payment",
        [
            SearchHit(artifact_id=public_id, score=2.0),
            SearchHit(artifact_id=restricted_id, score=5.0),
        ],
    )
    deps = make_broker_deps(factory, search, budget_policy=_policy(4, 10_000))
    request = KbSearchRequest(query="payment")

    outsider = await kb_search(deps, request, REQUESTER, session_key=SESSION)
    assert [hit.title for hit in outsider.results] == ["Public payment doc"]

    member = Requester(subject="payments-dev", teams=frozenset({"payments-team"}))
    insider = await kb_search(deps, request, member, session_key="mcp-session-2")
    assert {hit.title for hit in insider.results} == {
        "Public payment doc",
        "Restricted payment runbook",
    }


async def test_parallel_burst_cannot_overrun_the_call_cap(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Check-then-charge is serialized per window (kb_agent.py's in-loop re-check):
    four concurrent calls against a 1-call cap yield exactly one approved retrieval."""
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_payment_artifact(session, search)
    deps = make_broker_deps(factory, search, budget_policy=_policy(1, 100_000))
    request = KbSearchRequest(query="payment validation")

    responses = await asyncio.gather(
        *(kb_search(deps, request, REQUESTER, session_key=SESSION) for _ in range(4))
    )

    assert len([response for response in responses if response.results]) == 1
    assert all(response.notice == BUDGET_SPENT_NOTICE for response in responses)
    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert sorted(row.status for row in rows) == ["approved", "denied", "denied", "denied"]


async def test_budget_windows_are_isolated_per_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A new MCP session (a new agent run/task) starts a fresh budget window."""
    search = FakeSearchClient()
    async with factory() as session:
        await _seed_payment_artifact(session, search)
    deps = make_broker_deps(factory, search, budget_policy=_policy(1, 100_000))
    request = KbSearchRequest(query="payment validation")

    spent = await kb_search(deps, request, REQUESTER, session_key="task-a")
    assert spent.budget_remaining.calls == 0
    blocked = await kb_search(deps, request, REQUESTER, session_key="task-a")
    assert blocked.results == []

    fresh = await kb_search(deps, request, REQUESTER, session_key="task-b")
    assert fresh.results
    assert fresh.budget_remaining.calls == 0  # its own window, spent independently


async def test_no_active_kb_version_errors_and_writes_an_error_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
    deps = make_broker_deps(factory, FakeSearchClient(), budget_policy=_policy(4, 3000))

    with pytest.raises(ToolError, match="no active kb_version"):
        await kb_search(deps, KbSearchRequest(query="payment"), REQUESTER, session_key=SESSION)

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in rows] == [("kb_search", "error")]


async def test_search_backend_crash_refunds_the_charge_and_writes_no_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unexpected mid-flight failure (the search backend down, not an
    anticipated ToolError) must never eat the agent's budget. kb_search itself
    must NOT write a ledger row for its own crash either — that is the uniform
    tool wrapper's job (mcp/tool_handlers.py); a row here too would double-ledger
    the same failed call once the wrapper adds its own.
    """
    search = RaisingSearchClient(fail_on="boom")
    async with factory() as session:
        await _seed_payment_artifact(session, search.inner)
    deps = make_broker_deps(factory, search, budget_policy=_policy(1, 100_000))

    with pytest.raises(RuntimeError, match="search backend unavailable"):
        await kb_search(deps, KbSearchRequest(query="boom"), REQUESTER, session_key=SESSION)

    window = deps.kb_search_usage.window_for(SESSION, SUBJECT)
    assert window.usage.requests == 0
    assert window.usage.tokens == 0
    async with factory() as session:
        assert await fetch_ledger_rows(session, NO_RUN_SENTINEL) == []

    # the refund actually restores the budget: a working call afterwards still
    # spends the agent's one allowed request, not its (already exhausted) second
    recovered = await kb_search(
        deps, KbSearchRequest(query="payment validation"), REQUESTER, session_key=SESSION
    )
    assert recovered.results
    assert recovered.budget_remaining.calls == 0
    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [row.status for row in rows] == ["approved"]


async def test_no_hit_search_still_charges_a_call_and_is_ledgered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A miss is still a call (kb_agent.py parity) — budget and ledger both move."""
    deps = make_broker_deps(factory, FakeSearchClient(), budget_policy=_policy(2, 3000))

    response = await kb_search(
        deps, KbSearchRequest(query="nothing matches this"), REQUESTER, session_key=SESSION
    )

    assert response.results == []
    assert response.notice is None  # empty is not "budget spent"
    assert response.budget_remaining.calls == 1
    assert response.budget_remaining.tokens == 3000  # nothing returned, nothing charged
    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in rows] == [("kb_search", "approved")]
