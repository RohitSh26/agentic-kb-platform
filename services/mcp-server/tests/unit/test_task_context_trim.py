"""synthesize_node's response-budget trim, hermetic (no DB, no search calls).

Pins the by-index trim semantics: the trim state IS the kept lists (popped from
the tail), never a value-membership filter over the originals. The old filter
(`p in trim_order[0]`) misbehaved on EQUAL-VALUED entries — popping one tail
copy left the remaining copies matching every original, so nothing left the
response until all copies were popped and then ALL of them vanished at once
(overtrim, and O(n^2) membership scans).
"""

import uuid

from mcp_test_support import make_session_factory

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps, BrokerSettings
from agentic_mcp_server.context_broker.task_context import _build_response, synthesize_node
from agentic_mcp_server.context_broker.task_context_nodes import (
    BlastResolution,
    ScopeResolution,
    TaskContextCtx,
    TaskContextState,
)
from agentic_mcp_server.domain.token_budget import estimate_tokens
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.task_context import (
    Convention,
    GetTaskContextRequest,
    PriorChange,
)

REQUESTER = Requester(subject="impl-agent", teams=frozenset())

DUPLICATE_PRIOR = PriorChange(
    commit_or_pr_id="abc123def456",
    summary="fix(payments): harden the validation path against duplicate submissions",
    evidence_ids=[uuid.UUID("00000000-0000-0000-0000-000000000001")],
)
CONVENTION = Convention(
    pattern="payments validators reject negative amounts and log every rejection",
    evidence_ids=[uuid.UUID("00000000-0000-0000-0000-000000000002")],
)


def _ctx(cap: int) -> TaskContextCtx:
    return TaskContextCtx(
        deps=BrokerDeps(
            session_factory=make_session_factory(),
            search_client=FakeSearchClient(),
            settings=BrokerSettings(task_context_max_tokens=cap),
        ),
        requester=REQUESTER,
        request=GetTaskContextRequest(task_description="fix the payment validation"),
        build_seq=1,
        kb_version="kb-trim-test",
    )


def _tokens_with_priors(count: int) -> int:
    """Serialized size of the synthesized response carrying `count` copies of
    the duplicate prior + the one convention — the same meter the node uses."""
    response = _build_response(
        scope=ScopeResolution(),
        blast=BlastResolution(),
        conventions=(CONVENTION,),
        prior=(DUPLICATE_PRIOR,) * count,
        open_questions=[],
        calls_used=0,
    )
    return estimate_tokens(response.model_dump_json())


async def test_trim_drops_exactly_one_tail_copy_of_equal_valued_duplicates() -> None:
    # cap fits 2 copies of the duplicate prior but not 3 ⇒ exactly ONE tail pop
    cap = _tokens_with_priors(2)
    assert _tokens_with_priors(3) > cap
    state: TaskContextState = {
        "ctx": _ctx(cap),
        "scope": ScopeResolution(),
        "blast": BlastResolution(),
        "conventions": (CONVENTION,),
        "prior_changes": (DUPLICATE_PRIOR,) * 3,
        "calls_used": 0,
    }

    update = await synthesize_node(state)

    response = update.get("response")
    assert response is not None
    assert response.budget_used.tokens <= cap
    # one duplicate popped from the tail; the equal-valued survivors stay
    assert len(response.similar_prior_changes) == 2
    # the trim never reached the higher-value conventions bucket
    assert response.conventions == [CONVENTION]


async def test_trim_is_a_no_op_when_the_response_fits() -> None:
    state: TaskContextState = {
        "ctx": _ctx(cap=_tokens_with_priors(3)),
        "scope": ScopeResolution(),
        "blast": BlastResolution(),
        "conventions": (CONVENTION,),
        "prior_changes": (DUPLICATE_PRIOR,) * 3,
        "calls_used": 0,
    }

    update = await synthesize_node(state)

    response = update.get("response")
    assert response is not None
    assert len(response.similar_prior_changes) == 3
    assert response.conventions == [CONVENTION]
