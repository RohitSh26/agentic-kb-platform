"""Per-step tracing (ADR-0032): get_task_context + kb_search spans land in trace_span.

Covers the acceptance criteria against a real (local) Postgres registry: one root span
per call plus one span per node that actually ran, all sharing one trace_id and pointing
at the root's span_id as parent; a deliberately-raising sink never fails the tool call
(and the call's own retrieval_event ledger row still reads "approved"); NullTraceSink is
the do-nothing default when no sink is wired; an InMemoryTraceSink fake proves the port
swap (the Langfuse-later seam) without touching Postgres at all.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    RaisingTraceSink,
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
from agentic_mcp_server.context_broker.kb_search import kb_search
from agentic_mcp_server.context_broker.task_context import get_task_context
from agentic_mcp_server.infrastructure.postgres.trace_spans import PostgresTraceSink
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.infrastructure.tracing.trace_sink import InMemoryTraceSink
from agentic_mcp_server.mcp.tool_schemas.search import KbSearchRequest
from agentic_mcp_server.mcp.tool_schemas.task_context import GetTaskContextRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

REQUESTER = Requester(subject="impl-agent", teams=frozenset())
SESSION = "mcp-session-tracing"


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await session.execute(text("DELETE FROM trace_span"))
        await session.commit()
        await insert_build_run(session, KB_VERSION, "active")
    yield


async def _seed_payment_doc(
    factory: async_sessionmaker[AsyncSession], search: FakeSearchClient
) -> None:
    async with factory() as session:
        artifact_id = await insert_artifact(
            session, title="Payment doc", body_text="Validation rules for payments."
        )
    search.seed("payment", [SearchHit(artifact_id=artifact_id, score=2.0)])


async def _span_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[tuple[uuid.UUID, str, str, str, uuid.UUID | None, str]]:
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT span_id, name, service, status, parent_span_id, trace_id"
                " FROM trace_span ORDER BY started_at"
            )
        )
        return [
            (row.span_id, row.name, row.service, row.status, row.parent_span_id, row.trace_id)
            for row in result
        ]


# ------------------------------------------------------------------- get_task_context


async def test_get_task_context_writes_one_root_span_and_one_per_node(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    async with factory() as session:
        artifact_id = await insert_artifact(
            session,
            title="payments.py",
            body_text="def validate(amount): ...",
            artifact_type="code_file",
            source_uri="github://org/repo/services/checkout/payments.py",
        )
    search.seed("payment", [SearchHit(artifact_id=artifact_id, score=2.0)])
    deps = make_broker_deps(factory, search, trace_sink=PostgresTraceSink(factory))

    await get_task_context(
        deps, GetTaskContextRequest(task_description="fix the payment validation"), REQUESTER
    )

    rows = await _span_rows(factory)
    names = {name for _, name, _, _, _, _ in rows}
    assert names == {
        "get_task_context",
        "resolve_scope",
        "blast_radius",
        "conventions",
        "similar_prior_changes",
        "synthesize",
    }
    assert all(service == "mcp-server" for _, _, service, _, _, _ in rows)
    assert all(status == "ok" for _, _, _, status, _, _ in rows)
    # exactly one trace_id groups the whole call
    assert len({trace_id for *_, trace_id in rows}) == 1
    root_span_id, _, _, _, root_parent, _ = next(
        row for row in rows if row[1] == "get_task_context"
    )
    assert root_parent is None
    node_parents = {parent for _, name, _, _, parent, _ in rows if name != "get_task_context"}
    assert node_parents == {root_span_id}  # every node points at the root


async def test_kb_search_writes_one_root_span_per_call(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    await _seed_payment_doc(factory, search)
    deps = make_broker_deps(factory, search, trace_sink=PostgresTraceSink(factory))

    await kb_search(deps, KbSearchRequest(query="payment"), REQUESTER, session_key=SESSION)

    rows = await _span_rows(factory)
    assert [row[1] for row in rows] == ["kb_search"]
    _, _, service, status, parent, _ = rows[0]
    assert service == "mcp-server"
    assert status == "ok"
    assert parent is None


# ------------------------------------------------------------------------- fail-soft


async def test_a_raising_trace_sink_does_not_fail_the_tool_call(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fail-soft boundary (ADR-0032 §3): a completely broken sink must never fail the
    call it observes, and the call's own ledger row must still read "approved"."""
    search = FakeSearchClient()
    await _seed_payment_doc(factory, search)
    deps = make_broker_deps(factory, search, trace_sink=RaisingTraceSink())

    response = await kb_search(
        deps, KbSearchRequest(query="payment"), REQUESTER, session_key=SESSION
    )

    assert response.results  # the call itself succeeded despite tracing being broken
    async with factory() as session:
        row = (await session.execute(text("SELECT status FROM retrieval_event"))).one()
    assert row.status == "approved"


# ---------------------------------------------------------------- default + port swap


async def test_null_trace_sink_is_the_default_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    search = FakeSearchClient()
    await _seed_payment_doc(factory, search)
    deps = make_broker_deps(factory, search)  # no trace_sink passed -> NullTraceSink default

    await kb_search(deps, KbSearchRequest(query="payment"), REQUESTER, session_key=SESSION)

    assert await _span_rows(factory) == []


async def test_in_memory_trace_sink_proves_the_port_swap(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Swap Postgres for a fake sink with zero code changes at the call site — the
    designated Langfuse-later seam (ADR-0032 §4)."""
    search = FakeSearchClient()
    await _seed_payment_doc(factory, search)
    fake = InMemoryTraceSink()
    deps = make_broker_deps(factory, search, trace_sink=fake)

    await kb_search(deps, KbSearchRequest(query="payment"), REQUESTER, session_key=SESSION)

    assert [span.name for span in fake.spans] == ["kb_search"]
    assert await _span_rows(factory) == []  # nothing touched Postgres
