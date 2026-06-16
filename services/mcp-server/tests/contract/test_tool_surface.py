"""Tool surface tests: registry-driven registration, contract validation, telemetry.

These use the in-process fastmcp client (no HTTP), which is exactly why they
must not assert anything about auth — that boundary is covered by
tests/integration/test_auth.py against the real HTTP app. Broker behavior
against a real registry lives in tests/integration/test_context_broker.py;
here we only exercise what is deterministic without a database.
"""

import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS


async def test_registered_tools_match_contract_registry(server: FastMCP) -> None:
    tools = await server.list_tools()
    # The wire name is the canonical (dotted) name with '.' -> '_' so OpenAI-style clients
    # accept it (mcp/server.py); every registered tool maps back to a contract entry.
    assert {tool.name for tool in tools} == {name.replace(".", "_") for name in TOOL_SCHEMAS}
    assert all(tool.description for tool in tools)  # every tool is described (clients require it)


async def test_bare_query_is_rejected_by_the_schema(server: FastMCP) -> None:
    """A bare {"query": ...} must die at contract validation, not in broker code."""
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "context_request_more", {"request": {"query": "give me everything"}}
            )
    message = str(excinfo.value)
    assert "unknown context_pack_id" not in message  # rejected before the broker ran
    assert "why_needed" in message  # validation names the missing justification


async def test_telemetry_emits_structured_line_per_request(
    server: FastMCP, caplog: pytest.LogCaptureFixture
) -> None:
    # the extra field fails contract validation deterministically (no DB), but
    # the middleware logs before validation, so the line still carries run_id
    with caplog.at_level(logging.INFO, logger="agentic_mcp_server.telemetry.middleware"):
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "ledger_list_retrievals",
                    {"request": {"run_id": "run-42", "unexpected_field": True}},
                )
    lines = [r.getMessage() for r in caplog.records if "event=mcp_request" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "tool=ledger_list_retrievals" in line
    assert "run_id=run-42" in line
    assert "agent=" in line
    assert "latency_ms=" in line
    assert "status=error" in line


async def test_telemetry_never_logs_an_unsafe_run_id(
    server: FastMCP, caplog: pytest.LogCaptureFixture
) -> None:
    """The middleware logs before contract validation, so it must sanitize itself."""
    forged = "x status=ok agent=spoofed\nevent=mcp_request forged=line"
    with caplog.at_level(logging.INFO, logger="agentic_mcp_server.telemetry.middleware"):
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool("ledger_list_retrievals", {"request": {"run_id": forged}})
    lines = [r.getMessage() for r in caplog.records if "event=mcp_request" in r.getMessage()]
    assert len(lines) == 1
    assert "run_id=<unsafe-run-id>" in lines[0]
    assert "spoofed" not in lines[0]
