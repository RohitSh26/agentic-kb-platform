"""get_review_draft behavior against a real (local) Postgres registry (PR-41, ADR-0031).

Covers the acceptance criteria end to end: a stored draft is returned intact
(byte-for-byte, verbatim jsonb round-trip), no draft is a clean not-found
envelope (never a tool error), exactly one retrieval_event row per call
(approved for both the found and not-found cases, error on an unexpected read
failure), the tool never writes to review_panel, and no kb_search-style
budget is ever charged. The final test proves the tool is not just registered
but actually *reachable* end-to-end over a real MCP call — the run-1 lesson:
registered != reachable (see mcp/schema_rejection_middleware.py's docstring
for the sibling bug this same discipline caught).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from broker_test_support import (
    clean_registry,
    clean_review_drafts,
    insert_review_draft,
    make_broker_deps,
    require_registry_schema,
    require_review_panel_schema,
)
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp_test_support import (
    AGENT_SUBJECT,
    MCP_PATH,
    TEST_DATABASE_URL,
    VALID_TOKEN,
    FakeVerifier,
    make_session_factory,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker import review_draft as review_draft_mod
from agentic_mcp_server.context_broker.budgets import AgentAllowance, BudgetPolicy
from agentic_mcp_server.context_broker.review_draft import get_review_draft
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp import tool_handlers
from agentic_mcp_server.mcp.server import build_server
from agentic_mcp_server.mcp.tool_handlers import make_handlers
from agentic_mcp_server.mcp.tool_schemas.review_draft import GetReviewDraftRequest

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())
SESSION = "mcp-session-review-draft"
REPO = "acme/platform"


def _draft_doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0.0",
        "draft_key": "acme/platform#7@deadbeef",
        "repo": REPO,
        "pr_number": 7,
        "head_sha": "deadbeef",
        "generated_at": "2026-07-03T12:00:00Z",
        "advisory_verdict": "approve",
        "lens_verdicts": {"bug": "approve", "security": "approve"},
        "findings": [
            {
                "severity": "minor",
                "finding": "missing docstring",
                "evidence_ids": ["services/foo.py:12"],
                "lenses": ["quality"],
                "disagreement": None,
                "suggested_comment": "add a docstring here",
            }
        ],
        "open_questions": [],
        "synthesis": {
            "schema_version": "1.0.0",
            "verdict": "approve",
            "findings": [],
            "open_questions": [],
        },
        "summary_markdown": "# Draft review (never published automatically)\n\nLooks fine.",
        "provenance": {
            "engine": "review-panel",
            "engine_version": "0.1.0",
            "model": "groq:llama-3.3-70b-versatile",
            "lenses": ["bug", "security", "quality", "test_coverage"],
            "kb_used": False,
        },
    }
    base.update(overrides)
    return base


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await require_review_panel_schema(session)
        await clean_review_drafts(session)
    yield


async def _ledger_rows(session: AsyncSession) -> list[tuple[str, str, str, str, dict[str, Any]]]:
    result = await session.execute(
        text(
            "SELECT tool_name, status, run_id, kb_version, details FROM retrieval_event"
            " WHERE tool_name = 'get_review_draft' ORDER BY created_at, retrieval_id"
        )
    )
    return [
        (row.tool_name, row.status, row.run_id, row.kb_version, row.details or {}) for row in result
    ]


async def _review_draft_row_count(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT count(*) FROM review_panel.review_draft"))
    return result.scalar_one()


# --------------------------------------------------------------------- found / not-found


async def test_stored_draft_is_returned_intact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    doc = _draft_doc()
    async with factory() as session:
        await insert_review_draft(session, repo=REPO, pr_number=7, head_sha="deadbeef", draft=doc)
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_review_draft(
        deps, GetReviewDraftRequest(repo=REPO, pr_number=7, head_sha="deadbeef"), REQUESTER
    )

    assert response.found is True
    assert response.draft is not None
    assert response.draft.draft_key == "acme/platform#7@deadbeef"
    assert response.draft.repo == REPO
    assert response.draft.pr_number == 7
    assert response.draft.head_sha == "deadbeef"
    assert response.draft.draft == doc  # returned intact, verbatim jsonb round-trip

    async with factory() as session:
        rows = await _ledger_rows(session)
    assert [(tool, status, run_id, kb_version) for tool, status, run_id, kb_version, _ in rows] == [
        ("get_review_draft", "approved", "-", "-")
    ]
    assert rows[0][4]["found"] is True


async def test_omitted_head_sha_returns_the_newest_stored_draft(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await insert_review_draft(
            session,
            repo=REPO,
            pr_number=7,
            head_sha="older-sha",
            draft=_draft_doc(head_sha="older-sha"),
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        await insert_review_draft(
            session,
            repo=REPO,
            pr_number=7,
            head_sha="newer-sha",
            draft=_draft_doc(head_sha="newer-sha"),
        )
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_review_draft(
        deps, GetReviewDraftRequest(repo=REPO, pr_number=7), REQUESTER
    )

    assert response.found is True
    assert response.draft is not None
    assert response.draft.head_sha == "newer-sha"


async def test_no_draft_is_a_clean_not_found_envelope_never_an_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_review_draft(
        deps, GetReviewDraftRequest(repo=REPO, pr_number=999), REQUESTER
    )

    assert response.found is False
    assert response.draft is None

    async with factory() as session:
        rows = await _ledger_rows(session)
    assert [(tool, status) for tool, status, _, _, _ in rows] == [("get_review_draft", "approved")]
    assert rows[0][4]["found"] is False


async def test_a_different_pr_number_on_the_same_repo_is_not_matched(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await insert_review_draft(
            session, repo=REPO, pr_number=7, head_sha="deadbeef", draft=_draft_doc()
        )
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_review_draft(
        deps, GetReviewDraftRequest(repo=REPO, pr_number=8), REQUESTER
    )
    assert response.found is False


async def test_an_unmatched_head_sha_is_not_found_even_though_the_pr_has_a_draft(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await insert_review_draft(
            session, repo=REPO, pr_number=7, head_sha="deadbeef", draft=_draft_doc()
        )
    deps = make_broker_deps(factory, FakeSearchClient())

    response = await get_review_draft(
        deps,
        GetReviewDraftRequest(repo=REPO, pr_number=7, head_sha="a-different-sha"),
        REQUESTER,
    )
    assert response.found is False


# ------------------------------------------------------------------------------ read-only


async def test_the_tool_never_writes_to_review_panel(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await insert_review_draft(
            session, repo=REPO, pr_number=7, head_sha="deadbeef", draft=_draft_doc()
        )
        before = await _review_draft_row_count(session)
    deps = make_broker_deps(factory, FakeSearchClient())

    for _ in range(3):
        await get_review_draft(
            deps,
            GetReviewDraftRequest(repo=REPO, pr_number=7, head_sha="deadbeef"),
            REQUESTER,
        )
    await get_review_draft(deps, GetReviewDraftRequest(repo=REPO, pr_number=404), REQUESTER)

    async with factory() as session:
        after = await _review_draft_row_count(session)
    assert after == before


# ------------------------------------------------------------------------- no budget charge


async def test_no_kb_search_style_budget_is_ever_charged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A subject with a ZERO kb_search allowance still gets its draft — this
    tool has no budget window at all (docs/contracts/review-panel.md
    "Fetching drafts over MCP": fetching a stored draft is not knowledge
    retrieval)."""
    async with factory() as session:
        await insert_review_draft(
            session, repo=REPO, pr_number=7, head_sha="deadbeef", draft=_draft_doc()
        )
    zero_budget = BudgetPolicy(allowances={SUBJECT: AgentAllowance(max_requests=0, max_tokens=0)})
    deps = make_broker_deps(factory, FakeSearchClient(), budget_policy=zero_budget)

    for _ in range(3):
        response = await get_review_draft(
            deps,
            GetReviewDraftRequest(repo=REPO, pr_number=7, head_sha="deadbeef"),
            REQUESTER,
        )
        assert response.found is True

    # no kb_search-style window was ever touched for this subject
    window = deps.kb_search_usage.window_for(SESSION, SUBJECT)
    assert window.usage.requests == 0
    assert window.usage.tokens == 0


# ------------------------------------------------------------------------------- error path


async def test_unexpected_read_failure_writes_one_error_row_and_propagates(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected DB failure (e.g. review_panel absent from this connection,
    the documented DATABASE_URL != REVIEW_PANEL_DATABASE_URL limitation) is
    ledgered exactly once by the uniform tool wrapper (mcp/tool_handlers.py's
    `_ledgered`) — the SAME pattern as kb_search's own crash path — and the
    exception still reaches the caller, distinct from the clean not-found case."""
    monkeypatch.setattr(tool_handlers, "current_requester", lambda: REQUESTER)
    monkeypatch.setattr(tool_handlers, "current_session_key", lambda: SESSION)
    monkeypatch.setattr(
        tool_handlers,
        "current_client_identity",
        lambda registry: ClientIdentity(client_id="test-client"),
    )

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError('relation "review_panel.review_draft" does not exist')

    monkeypatch.setattr(review_draft_mod, "fetch_review_draft", _boom)
    deps = make_broker_deps(factory, FakeSearchClient())
    handlers = make_handlers(deps)

    with pytest.raises(RuntimeError, match="does not exist"):
        await handlers["get_review_draft"](
            GetReviewDraftRequest(repo=REPO, pr_number=7, head_sha="deadbeef")
        )

    async with factory() as session:
        rows = await _ledger_rows(session)
    assert [(tool, status) for tool, status, _, _, _ in rows] == [("get_review_draft", "error")]
    assert rows[0][4] == {"exception_type": "RuntimeError"}


# ---------------------------------------------------------------- reachable end-to-end (wire)


@asynccontextmanager
async def _connected_client(server: FastMCP) -> AsyncIterator[Client]:
    """One real MCP session over the ASGI-in-process HTTP transport (real auth,
    real fastmcp dispatch) — mirrors test_schema_rejection_ledger.py's helper."""
    app = server.http_app(path=MCP_PATH, stateless_http=True)

    def client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        **kwargs: object,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=True,
        )

    transport = StreamableHttpTransport(
        url=f"http://testserver{MCP_PATH}",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        httpx_client_factory=client_factory,
    )
    async with app.router.lifespan_context(app), Client(transport) as client:
        yield client


async def test_reachable_end_to_end_over_a_real_mcp_call(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Registered != reachable (the run-1 lesson, PR-41 brief): being present in
    TOOL_SCHEMAS proves nothing about the handler actually being wired in
    mcp/tool_handlers.py. This drives a real bearer-authenticated MCP call all
    the way through fastmcp dispatch, the broker, and a real Postgres read."""
    doc = _draft_doc()
    async with factory() as session:
        await insert_review_draft(session, repo=REPO, pr_number=7, head_sha="deadbeef", draft=doc)
    server = build_server(auth=FakeVerifier(), session_factory=factory)

    async with _connected_client(server) as client:
        result = await client.call_tool(
            "get_review_draft",
            {"request": {"repo": REPO, "pr_number": 7, "head_sha": "deadbeef"}},
        )

    assert result.structured_content is not None
    assert result.structured_content["found"] is True
    assert result.structured_content["draft"]["draft_key"] == "acme/platform#7@deadbeef"
    assert result.structured_content["draft"]["draft"] == doc

    async with factory() as session:
        rows = await _ledger_rows(session)
    assert [(tool, status) for tool, status, _, _, _ in rows] == [("get_review_draft", "approved")]
    async with factory() as session:
        agent_row = (
            await session.execute(
                text("SELECT agent_name FROM retrieval_event WHERE tool_name = 'get_review_draft'")
            )
        ).one()
    assert agent_row.agent_name == AGENT_SUBJECT
