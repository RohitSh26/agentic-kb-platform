"""LangGraph structure tests for get_task_context (PR-39, ADR-0030 §2).

Hermetic (no database, no LangSmith env): the fan-out is proven genuinely
concurrent with a rendezvous SearchClient that only unblocks once all four
resolution nodes have entered their first retrieval — a sequential graph would
deadlock and fail the wait_for. The broadened retry is pinned to fire exactly
once, only on truly-empty scope. The whole module runs with every LANGSMITH_*
variable removed, per the brief's "suite green with no LangSmith creds" gate.
"""

import asyncio
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field

import pytest
from mcp_test_support import make_session_factory

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.task_context import (
    _route_after_synthesize,
    run_task_context_graph,
    tracing_enabled,
)
from agentic_mcp_server.context_broker.task_context_nodes import (
    AmbiguousCandidate,
    ScopeEntity,
    ScopeResolution,
    TaskContextCtx,
    TaskContextState,
)
from agentic_mcp_server.infrastructure.search.search_client import (
    FakeSearchClient,
    SearchClient,
    SearchHit,
)
from agentic_mcp_server.mcp.tool_schemas.task_context import GetTaskContextRequest

REQUESTER = Requester(subject="impl-agent", teams=frozenset())
PARALLEL_NODES = {"resolve_scope", "blast_radius", "conventions", "similar_prior_changes"}


@pytest.fixture(autouse=True)
def _no_langsmith_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # the gate under test: everything here must pass with NO LangSmith env at all
    for name in [key for key in os.environ if key.startswith("LANGSMITH_")]:
        monkeypatch.delenv(name, raising=False)


def _ctx(search: SearchClient, task: str = "find the payment validator") -> TaskContextCtx:
    return TaskContextCtx(
        deps=BrokerDeps(session_factory=make_session_factory(), search_client=search),
        requester=REQUESTER,
        request=GetTaskContextRequest(task_description=task),
        build_seq=1,
        kb_version="kb-structure-test",
    )


@dataclass
class RendezvousSearchClient:
    """Blocks each of the first four search calls until ALL four have arrived.

    Only a genuinely parallel fan-out (each node issuing its first retrieval in
    the same superstep) can release the rendezvous; any sequential execution
    deadlocks here and trips the test's timeout instead of passing.
    """

    barrier: asyncio.Barrier = field(default_factory=lambda: asyncio.Barrier(4))
    first_calls_seen: int = 0

    async def search(self, query: str, *, build_seq: int, top: int) -> list[SearchHit]:
        if self.first_calls_seen < 4:
            self.first_calls_seen += 1
            await self.barrier.wait()
        return []


async def test_the_four_resolution_nodes_run_concurrently() -> None:
    search = RendezvousSearchClient()
    run = await asyncio.wait_for(run_task_context_graph(_ctx(search)), timeout=10.0)
    assert search.first_calls_seen == 4
    # all four fan-out nodes ran (twice: the empty KB also triggers the retry)
    names = {span.node for span in run.node_spans}
    assert names >= PARALLEL_NODES


async def test_empty_scope_triggers_exactly_one_broadened_retry() -> None:
    run = await run_task_context_graph(_ctx(FakeSearchClient()))  # no hits anywhere

    assert run.retried is True
    counts = Counter(span.node for span in run.node_spans)
    assert counts["broaden"] == 1, "the broadened retry must fire exactly once"
    for node in PARALLEL_NODES:
        assert counts[node] == 2, f"{node} must re-run exactly once for the retry"
    assert counts["synthesize"] == 2
    # the final answer is honest about the gap — an open question, not an invention
    assert run.response.resolved_scope.entities == []
    assert any("broadened" in question for question in run.response.open_questions)


async def test_retry_never_fires_twice_and_the_router_stops_on_any_answer() -> None:
    ctx = _ctx(FakeSearchClient())
    entity = ScopeEntity(
        entity_id=uuid.uuid4(),
        path="services/x/payments.py",
        symbol=None,
        resolution_source="search",
        confidence_tier="interpreted",
    )
    ambiguous = AmbiguousCandidate(
        alias_text="payments",
        candidates=[uuid.uuid4(), uuid.uuid4()],
        reason="two same-named definitions",
    )

    def route(scope: ScopeResolution, *, broadened: bool) -> str:
        state: TaskContextState = {"ctx": ctx, "scope": scope, "broadened": broadened}
        return _route_after_synthesize(state)

    # resolved scope ⇒ answer now
    assert route(ScopeResolution(entities=(entity,)), broadened=False) == "__end__"
    # ambiguity IS an answer (candidates + open questions) — it must not retry
    assert route(ScopeResolution(ambiguous=(ambiguous,)), broadened=False) == "__end__"
    # truly empty ⇒ one broadened retry ...
    assert route(ScopeResolution(), broadened=False) == "broaden"
    # ... and never a second one
    assert route(ScopeResolution(), broadened=True) == "__end__"


async def test_graph_reports_retrieval_calls_and_spans_for_the_ledger() -> None:
    run = await run_task_context_graph(_ctx(FakeSearchClient()))
    # every span is a closed window; the ledger derives node_latency_ms from these
    assert all(span.ended >= span.started for span in run.node_spans)
    # each fan-out node issued at least one metered retrieval per pass
    assert run.calls_used >= len(PARALLEL_NODES)
    assert run.response.budget_used.calls == run.calls_used


def test_tracing_is_env_gated_and_off_by_default() -> None:
    assert tracing_enabled() is False  # autouse fixture removed all LANGSMITH_*


def test_tracing_activates_via_the_documented_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for value, expected in (("true", True), ("1", True), ("false", False), ("", False)):
        monkeypatch.setenv("LANGSMITH_TRACING", value)
        assert tracing_enabled() is expected
