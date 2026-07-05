"""The uniform error-ledger wrapper (mcp/tool_handlers.py, PR "ledger complete
by construction"): every tool call ends in exactly one retrieval_event row —
approved, denied, or error — an unexpected mid-flight crash never eats a
budget charge it made, a ledger-write failure while handling an error never
masks the original failure, and the exception always still reaches the
caller (the MCP client / host needs to know the call failed so it falls back
to files, ADR-0025 §4).

Exercises make_handlers' composed pipeline (client-scope gate -> broker call
-> _ledgered's uniform wrapper) directly, with identity resolution
monkeypatched to fixed test values — the fastmcp request/auth/JSON-RPC
boundary itself is covered by test_auth.py and test_tool_surface.py and is
not this file's concern.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import cast

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

from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker import evidence as evidence_module
from agentic_mcp_server.context_broker import expand as expand_module
from agentic_mcp_server.context_broker import request_more as request_more_module
from agentic_mcp_server.context_broker.budgets import AgentAllowance, BudgetPolicy
from agentic_mcp_server.context_broker.constants import NO_RUN_SENTINEL
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    insert_event as real_insert_event,
)
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp import tool_handlers
from agentic_mcp_server.mcp.tool_handlers import make_handlers
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    CreatePackResponse,
    ExpandRequest,
    ExpandResponse,
    OpenEvidenceRequest,
    OpenEvidenceResponse,
    RequestMoreRequest,
    RequestMoreResponse,
)
from agentic_mcp_server.mcp.tool_schemas.search import KbSearchRequest, KbSearchResponse

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())
SESSION = "mcp-session-wrapper"


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


@pytest.fixture(autouse=True)
def _fixed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise the wrapped handler pipeline directly, bypassing the
    # fastmcp request-scoped context (get_access_token/get_context) that
    # current_requester/current_client_identity/current_session_key normally
    # read from a live MCP session — that boundary is test_auth.py's concern.
    monkeypatch.setattr(tool_handlers, "current_requester", lambda: REQUESTER)
    monkeypatch.setattr(tool_handlers, "current_session_key", lambda: SESSION)
    monkeypatch.setattr(
        tool_handlers,
        "current_client_identity",
        lambda registry: ClientIdentity(client_id="test-client"),
    )


async def _fetch_details(session: AsyncSession, tool_name: str) -> list[dict[str, object]]:
    result = await session.execute(
        text(
            "SELECT details FROM retrieval_event WHERE tool_name = :tool_name"
            " ORDER BY created_at, retrieval_id"
        ),
        {"tool_name": tool_name},
    )
    return [row.details for row in result]


PACK_RUN_ID = "run-wrapper-pack"


async def _make_pack(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, tool_handlers.HandlerFn], FakeSearchClient, uuid.UUID, str]:
    """A wrapped-handler pipeline with one org-public artifact seeded and a pack
    created for it through the wrapped context.create_pack handler — the
    shared setup the three pack-tool crash tests below build on. Returns
    (handlers, search, artifact_id, context_pack_id); the search client is
    returned so a request_more crash test can seed a follow-up hit."""
    search = FakeSearchClient()
    async with factory() as session:
        artifact_id = await insert_artifact(
            session,
            title="Payment validation rules",
            body_text="Validation lives in checkout/validators.py and rejects negative amounts.",
        )
    search.seed("payment", [SearchHit(artifact_id=artifact_id, score=2.0)])
    deps = make_broker_deps(factory, search, budget_policy=_policy(4, 100_000))
    handlers = make_handlers(deps)
    created = cast(
        CreatePackResponse,
        await handlers["context.create_pack"](
            CreatePackRequest(
                run_id=PACK_RUN_ID,
                task="payment validation",
                approved_context_plan="review the payment validation rules for checkout",
                retrieval_profile="default",
                budget_tokens=8000,
            )
        ),
    )
    return handlers, search, artifact_id, created.context_pack_id


async def test_unexpected_exception_writes_one_error_row_refunds_and_propagates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The point: a platform crash mid-call is ledgered exactly once, its
    budget charge is refunded, and the exception still reaches the caller."""
    search = RaisingSearchClient(fail_on="boom")
    deps = make_broker_deps(factory, search, budget_policy=_policy(1, 100_000))
    handlers = make_handlers(deps)

    with pytest.raises(RuntimeError, match="search backend unavailable"):
        await handlers["kb_search"](KbSearchRequest(query="boom"))

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in rows] == [("kb_search", "error")]
    assert rows[0].agent_name == SUBJECT
    async with factory() as session:
        details = await _fetch_details(session, "kb_search")
    assert details == [{"exception_type": "RuntimeError"}]

    # budget refunded: a working call afterwards still spends the agent's ONE
    # allowed request, not its (already exhausted) second
    recovered = cast(
        KbSearchResponse,
        await handlers["kb_search"](KbSearchRequest(query="payment validation")),
    )
    assert recovered.budget_remaining.calls == 0
    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [row.status for row in rows] == ["error", "approved"]


async def test_open_evidence_crash_writes_one_error_row_refunds_and_propagates(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same crash-refund guarantee as kb_search, exercised through the wrapped
    context.open_evidence handler (Fix: pack-scoped crash refunds, kb_search's
    precedent commit 346c2d2): a platform crash mid-call is ledgered exactly
    once (by the wrapper, not open_evidence itself), its pack budget charge is
    refunded, and the exception still reaches the caller."""
    handlers, _search, artifact_id, context_pack_id = await _make_pack(factory)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger db down")

    monkeypatch.setattr(evidence_module, "insert_event", _boom)

    with pytest.raises(RuntimeError, match="ledger db down"):
        await handlers["context.open_evidence"](
            OpenEvidenceRequest(
                context_pack_id=context_pack_id, evidence_id=str(artifact_id), max_tokens=1000
            )
        )

    # the pack's own run_id ledger is untouched by the crash — no orphan/half
    # row lands there; the wrapper's generic error row carries no run handle
    # (it has no pack/domain context, only the exception), so it lands under
    # the sentinel, exactly like kb_search's own error rows.
    async with factory() as session:
        pack_run_rows = await fetch_ledger_rows(session, PACK_RUN_ID)
    assert [(row.tool_name, row.status) for row in pack_run_rows] == [
        ("context.create_pack", "approved")
    ]
    async with factory() as session:
        error_rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in error_rows] == [
        ("context.open_evidence", "error")
    ]
    async with factory() as session:
        details = await _fetch_details(session, "context.open_evidence")
    assert details == [{"exception_type": "RuntimeError"}]

    # budget refunded: a working call afterwards still charges normally. Restore
    # just this one attribute (not monkeypatch.undo(), which is shared with the
    # autouse _fixed_identity fixture and would strip the identity patches too).
    monkeypatch.setattr(evidence_module, "insert_event", real_insert_event)
    recovered = cast(
        OpenEvidenceResponse,
        await handlers["context.open_evidence"](
            OpenEvidenceRequest(
                context_pack_id=context_pack_id, evidence_id=str(artifact_id), max_tokens=1000
            )
        ),
    )
    assert recovered.tokens_used > 0
    async with factory() as session:
        pack_run_rows = await fetch_ledger_rows(session, PACK_RUN_ID)
    assert [row.status for row in pack_run_rows] == ["approved", "approved"]


async def test_request_more_crash_writes_one_error_row_refunds_and_propagates(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same crash-refund guarantee, exercised through the wrapped
    context.request_more handler."""
    handlers, search, _artifact_id, context_pack_id = await _make_pack(factory)
    async with factory() as session:
        refund_id = await insert_artifact(
            session,
            title="Refund processing",
            body_text="Refunds are processed by checkout/refunds.py within 24 hours.",
        )
    search.seed("refund", [SearchHit(artifact_id=refund_id, score=2.0)])

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger db down")

    monkeypatch.setattr(request_more_module, "insert_event", _boom)

    def _request(question: str) -> RequestMoreRequest:
        return RequestMoreRequest(
            context_pack_id=context_pack_id,
            agent_name="impl-agent-manifest",
            question=question,
            why_needed="the pack does not cover it",
            decision_needed="which module to extend",
            already_checked_evidence_ids=[],
            max_tokens=1500,
        )

    question = "how does refund processing work in checkout"
    with pytest.raises(RuntimeError, match="ledger db down"):
        await handlers["context.request_more"](_request(question))

    async with factory() as session:
        pack_run_rows = await fetch_ledger_rows(session, PACK_RUN_ID)
    assert [(row.tool_name, row.status) for row in pack_run_rows] == [
        ("context.create_pack", "approved")
    ]
    async with factory() as session:
        error_rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in error_rows] == [
        ("context.request_more", "error")
    ]
    async with factory() as session:
        details = await _fetch_details(session, "context.request_more")
    assert details == [{"exception_type": "RuntimeError"}]

    # budget refunded: a working call afterwards still resolves and charges.
    # Restore just this one attribute (not monkeypatch.undo(), which is shared
    # with the autouse _fixed_identity fixture and would strip the identity
    # patches too).
    monkeypatch.setattr(request_more_module, "insert_event", real_insert_event)
    recovered = cast(
        RequestMoreResponse,
        await handlers["context.request_more"](_request(question)),
    )
    assert recovered.status == "approved"
    assert [c.evidence_id for c in recovered.new_evidence_cards] == [str(refund_id)]
    async with factory() as session:
        pack_run_rows = await fetch_ledger_rows(session, PACK_RUN_ID)
    assert [row.status for row in pack_run_rows] == ["approved", "approved"]


async def test_expand_crash_writes_one_error_row_refunds_and_propagates(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same crash-refund guarantee, exercised through the wrapped
    context.expand handler."""
    handlers, _search, _artifact_id, context_pack_id = await _make_pack(factory)
    async with factory() as session:
        neighbor_id = await insert_artifact(
            session, title="Refund processing", body_text="Refunds within 24 hours."
        )

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger db down")

    monkeypatch.setattr(expand_module, "insert_event", _boom)

    def _expand_request() -> ExpandRequest:
        return ExpandRequest(
            seed_artifact_ids=[neighbor_id],
            budget_tokens=10_000,
            context_pack_id=context_pack_id,
        )

    with pytest.raises(RuntimeError, match="ledger db down"):
        await handlers["context.expand"](_expand_request())

    async with factory() as session:
        pack_run_rows = await fetch_ledger_rows(session, PACK_RUN_ID)
    assert [(row.tool_name, row.status) for row in pack_run_rows] == [
        ("context.create_pack", "approved")
    ]
    async with factory() as session:
        error_rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in error_rows] == [("context.expand", "error")]
    async with factory() as session:
        details = await _fetch_details(session, "context.expand")
    assert details == [{"exception_type": "RuntimeError"}]

    # budget refunded: a working call afterwards still charges normally.
    # Restore just this one attribute (not monkeypatch.undo(), which is shared
    # with the autouse _fixed_identity fixture and would strip the identity
    # patches too).
    monkeypatch.setattr(expand_module, "insert_event", real_insert_event)
    recovered = cast(ExpandResponse, await handlers["context.expand"](_expand_request()))
    assert recovered.tokens_used > 0
    async with factory() as session:
        pack_run_rows = await fetch_ledger_rows(session, PACK_RUN_ID)
    assert [row.status for row in pack_run_rows] == ["approved", "approved"]


async def test_anticipated_failure_is_not_double_ledgered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """kb_search's own no-active-version path already writes its error row
    (LedgeredToolError) — the wrapper must not add a second one for it."""
    async with factory() as session:
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
    deps = make_broker_deps(
        factory, RaisingSearchClient(fail_on="unused"), budget_policy=_policy(4, 3000)
    )
    handlers = make_handlers(deps)

    with pytest.raises(ToolError, match="no active kb_version"):
        await handlers["kb_search"](KbSearchRequest(query="payment"))

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in rows] == [("kb_search", "error")]


async def test_ledger_write_failure_during_error_handling_surfaces_original_error(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-soft: if the wrapper's own error-ledger write ALSO fails (DB fully
    down), the original exception still surfaces — never masked by the
    ledger-write failure — and the failure is logged with structured fields."""
    search = RaisingSearchClient(fail_on="boom")
    deps = make_broker_deps(factory, search, budget_policy=_policy(1, 100_000))
    handlers = make_handlers(deps)

    async def _write_error_event_boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger db down")

    monkeypatch.setattr(tool_handlers, "write_error_event", _write_error_event_boom)

    with (
        caplog.at_level(logging.ERROR, logger="agentic_mcp_server.mcp.tool_handlers"),
        pytest.raises(RuntimeError, match="search backend unavailable"),
    ):
        await handlers["kb_search"](KbSearchRequest(query="boom"))

    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "event=error_ledger_write_failed" in line
        and "tool_name=kb_search" in line
        and "exception_type=RuntimeError" in line
        and "ledger_exception_type=RuntimeError" in line
        for line in lines
    )
    # the ledger write itself failed, so no row at all — never a half-written one
    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert rows == []
