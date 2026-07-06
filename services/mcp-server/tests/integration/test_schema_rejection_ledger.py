"""SchemaRejectionLedgerMiddleware (mcp/schema_rejection_middleware.py): the MCP
boundary's ledger-completeness guarantee for calls fastmcp itself rejects at
schema validation — before ANY handler, and so before ``_ledgered``
(mcp/tool_handlers.py), ever runs.

docs/reports/host-integration-2026-07-06.md finding 4: a real host's malformed
`kb_search` call (verbatim pydantic-rejected shape reproduced below) left no
`retrieval_event` row at all, violating "the ledger is complete by
construction" (mcp-tools-contract.md). These tests go through the real HTTP
ASGI boundary (StreamableHttpTransport), exactly where fastmcp's own argument
validation fires — an in-process client or a direct handler call cannot
reproduce this bug (see test_error_ledger_wrapper.py's docstring for why that
file exercises the wrapped-handler pipeline directly instead).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import httpx
import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    fetch_ledger_rows,
    insert_artifact,
    insert_build_run,
    require_registry_schema,
)
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
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

from agentic_mcp_server.context_broker.budgets import AgentAllowance, BudgetPolicy
from agentic_mcp_server.context_broker.constants import NO_RUN_SENTINEL
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp import schema_rejection_middleware
from agentic_mcp_server.mcp.server import build_server

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

# The verbatim malformed shape from the host-integration report: a typo'd field
# name and no "request" wrapper at all, so fastmcp's own type adapter rejects
# it with a missing-argument + unexpected-keyword-argument pair before any
# handler in tool_handlers.py runs.
MALFORMED_KB_SEARCH_ARGS = {"quer y": 1}


def _policy(max_requests: int, max_tokens: int) -> BudgetPolicy:
    return BudgetPolicy(
        allowances={AGENT_SUBJECT: AgentAllowance(max_requests=max_requests, max_tokens=max_tokens)}
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


@asynccontextmanager
async def _connected_client(server: FastMCP) -> AsyncIterator[Client]:
    """One real MCP session over the ASGI-in-process HTTP transport.

    Both calls a test makes inside this context share one MCP session (the
    kb_search budget window key, ADR-0025 §4), exactly like a real host
    reusing one connection across a malformed call and a retry.
    """
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


async def _fetch_details(session: AsyncSession, tool_name: str) -> list[dict[str, object]]:
    result = await session.execute(
        text(
            "SELECT details FROM retrieval_event WHERE tool_name = :tool_name"
            " ORDER BY created_at, retrieval_id"
        ),
        {"tool_name": tool_name},
    )
    return [row.details for row in result]


async def _seed_payment_artifact(
    factory: async_sessionmaker[AsyncSession], search: FakeSearchClient
) -> None:
    async with factory() as session:
        artifact_id = await insert_artifact(
            session,
            title="Payment validation rules",
            body_text="Validation lives in checkout/validators.py and rejects negatives.",
        )
    search.seed("payment", [SearchHit(artifact_id=artifact_id, score=2.0)])


async def test_schema_rejected_call_writes_one_error_row_and_charges_no_budget(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The point of the fix: a malformed call is ledgered exactly once, the
    validation error still reaches the client unchanged, and — because no
    handler ever ran — it spends none of the caller's budget. A single-request
    allowance proves the last claim: if the rejected call HAD charged, the
    valid call right after would be denied instead of approved."""
    search = FakeSearchClient()
    await _seed_payment_artifact(factory, search)
    server = build_server(
        auth=FakeVerifier(),
        session_factory=factory,
        search_client=search,
        budget_policy=_policy(max_requests=1, max_tokens=100_000),
    )

    async with _connected_client(server) as client:
        with pytest.raises(ToolError, match="validation error"):
            await client.call_tool("kb_search", MALFORMED_KB_SEARCH_ARGS)

        async with factory() as session:
            rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
        assert [(row.tool_name, row.status) for row in rows] == [("kb_search", "error")]
        assert rows[0].agent_name == AGENT_SUBJECT

        # zero budget charge: the one allowed request is still available
        result = await client.call_tool("kb_search", {"request": {"query": "payment validation"}})

    assert result.structured_content is not None
    assert result.structured_content["budget_remaining"]["calls"] == 0
    assert len(result.structured_content["results"]) == 1

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in rows] == [
        ("kb_search", "error"),
        ("kb_search", "approved"),
    ]


async def test_schema_rejected_call_details_omit_raw_argument_values(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """details carries the exception type + a terse validation summary, never
    the raw argument values a host sent (hosts may put anything in them)."""
    search = FakeSearchClient()
    server = build_server(
        auth=FakeVerifier(),
        session_factory=factory,
        search_client=search,
        budget_policy=_policy(max_requests=4, max_tokens=100_000),
    )

    async with _connected_client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("kb_search", MALFORMED_KB_SEARCH_ARGS)

    async with factory() as session:
        details_rows = await _fetch_details(session, "kb_search")
    [details] = details_rows
    assert details["exception_type"] == "ValidationError"
    errors = cast(list[dict[str, str]], details["validation_errors"])
    assert {"loc", "type", "msg"} == set().union(*(error.keys() for error in errors))
    dumped = str(errors)
    assert "quer y" in dumped  # the offending field NAME is fine (schema-shape info)
    assert "input" not in dumped  # pydantic's own error dicts carry raw values under "input"


async def test_valid_call_still_writes_exactly_one_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Baseline: the new middleware must not perturb the happy path — a
    normal, schema-valid call still ends in exactly one ledger row."""
    search = FakeSearchClient()
    await _seed_payment_artifact(factory, search)
    server = build_server(
        auth=FakeVerifier(),
        session_factory=factory,
        search_client=search,
        budget_policy=_policy(max_requests=4, max_tokens=100_000),
    )

    async with _connected_client(server) as client:
        result = await client.call_tool("kb_search", {"request": {"query": "payment validation"}})
    assert result.structured_content is not None
    assert len(result.structured_content["results"]) == 1

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert [(row.tool_name, row.status) for row in rows] == [("kb_search", "approved")]


async def test_ledger_write_failure_during_rejection_surfaces_original_validation_error(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-soft, identical discipline to _write_unexpected_error: if the
    ledger write for a schema-rejected call ALSO fails (DB fully down), the
    original validation error still surfaces — never masked — and the
    failure is logged with structured fields. No half-written row remains."""
    search = FakeSearchClient()
    server = build_server(
        auth=FakeVerifier(),
        session_factory=factory,
        search_client=search,
        budget_policy=_policy(max_requests=4, max_tokens=100_000),
    )

    async def _write_error_event_boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger db down")

    monkeypatch.setattr(schema_rejection_middleware, "write_error_event", _write_error_event_boom)

    with caplog.at_level(
        logging.ERROR, logger="agentic_mcp_server.mcp.schema_rejection_middleware"
    ):
        async with _connected_client(server) as client:
            with pytest.raises(ToolError, match="validation error"):
                await client.call_tool("kb_search", MALFORMED_KB_SEARCH_ARGS)

    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "event=error_ledger_write_failed" in line
        and "tool_name=kb_search" in line
        and "exception_type=ValidationError" in line
        and "ledger_exception_type=RuntimeError" in line
        for line in lines
    )
    # the ledger write itself failed, so no row at all — never a half-written one
    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert rows == []
