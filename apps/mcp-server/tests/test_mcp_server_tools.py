"""Tool surface tests: registry-driven registration, contract validation, stubs.

These use the in-process fastmcp client (no HTTP), which is exactly why they
must not assert anything about auth — that boundary is covered by
test_mcp_server_auth.py against the real HTTP app.
"""

import logging
import uuid
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from contracts.mcp_schemas import TOOL_SCHEMAS

ARTIFACT_ID = str(uuid.uuid4())


async def test_registered_tools_match_contract_registry(server: FastMCP) -> None:
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == set(TOOL_SCHEMAS)


async def test_every_stub_returns_not_implemented(server: FastMCP) -> None:
    valid_requests: dict[str, dict[str, Any]] = {
        "context.create_pack": {
            "run_id": "run-1",
            "task": "add endpoint",
            "approved_context_plan": "plan",
            "retrieval_profile": "default",
            "budget_tokens": 8000,
        },
        "context.read_pack": {"context_pack_id": "pack-1", "role": "implementation"},
        "context.request_more": {
            "context_pack_id": "pack-1",
            "agent_name": "impl-agent",
            "question": "what validates the payload?",
            "why_needed": "to reuse the existing validator",
            "decision_needed": "which module to extend",
            "already_checked_evidence_ids": ["ev-1"],
            "max_tokens": 1500,
        },
        "context.open_evidence": {
            "context_pack_id": "pack-1",
            "evidence_id": "ev-1",
            "max_tokens": 800,
        },
        "graph.get_neighbors": {"artifact_id": ARTIFACT_ID},
        "ledger.list_retrievals": {"run_id": "run-1"},
    }
    assert set(valid_requests) == set(TOOL_SCHEMAS)
    async with Client(server) as client:
        for tool_name, request in valid_requests.items():
            with pytest.raises(ToolError, match="not implemented"):
                await client.call_tool(tool_name, {"request": request})


async def test_bare_query_is_rejected_by_the_schema(server: FastMCP) -> None:
    """A bare {"query": ...} must die at contract validation, not in broker code."""
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "context.request_more", {"request": {"query": "give me everything"}}
            )
    message = str(excinfo.value)
    assert "not implemented" not in message  # rejected before the stub ran
    assert "why_needed" in message  # validation names the missing justification


async def test_telemetry_emits_structured_line_per_request(
    server: FastMCP, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="mcp_server.telemetry"):
        async with Client(server) as client:
            with pytest.raises(ToolError, match="not implemented"):
                await client.call_tool("ledger.list_retrievals", {"request": {"run_id": "run-42"}})
    lines = [r.getMessage() for r in caplog.records if "event=mcp_request" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "tool=ledger.list_retrievals" in line
    assert "run_id=run-42" in line
    assert "agent=" in line
    assert "latency_ms=" in line
    assert "status=error" in line


async def test_telemetry_never_logs_an_unsafe_run_id(
    server: FastMCP, caplog: pytest.LogCaptureFixture
) -> None:
    """The middleware logs before contract validation, so it must sanitize itself."""
    forged = "x status=ok agent=spoofed\nevent=mcp_request forged=line"
    with caplog.at_level(logging.INFO, logger="mcp_server.telemetry"):
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool("ledger.list_retrievals", {"request": {"run_id": forged}})
    lines = [r.getMessage() for r in caplog.records if "event=mcp_request" in r.getMessage()]
    assert len(lines) == 1
    assert "run_id=<unsafe-run-id>" in lines[0]
    assert "spoofed" not in lines[0]
