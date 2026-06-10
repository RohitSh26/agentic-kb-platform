"""Auth boundary tests: unauthenticated calls never reach a tool.

These go through the real HTTP app (httpx ASGI transport, in process), because
that is where fastmcp enforces bearer auth — an in-process client would bypass
the boundary under test.
"""

import logging
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp_test_support import AGENT_SUBJECT, MCP_PATH, VALID_TOKEN, asgi_http_client

INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    },
}
ACCEPT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


async def test_missing_token_is_rejected(server: FastMCP) -> None:
    async with asgi_http_client(server) as http_client:
        response = await http_client.post(MCP_PATH, json=INITIALIZE_BODY, headers=ACCEPT_HEADERS)
    assert response.status_code == 401


async def test_invalid_token_is_rejected(server: FastMCP) -> None:
    async with asgi_http_client(server) as http_client:
        response = await http_client.post(
            MCP_PATH,
            json=INITIALIZE_BODY,
            headers={**ACCEPT_HEADERS, "Authorization": "Bearer forged-token"},
        )
    assert response.status_code == 401


async def test_valid_token_is_accepted(server: FastMCP) -> None:
    async with asgi_http_client(server) as http_client:
        response = await http_client.post(
            MCP_PATH,
            json=INITIALIZE_BODY,
            headers={**ACCEPT_HEADERS, "Authorization": f"Bearer {VALID_TOKEN}"},
        )
    assert response.status_code == 200


async def test_authenticated_tool_call_logs_agent_identity(
    server: FastMCP, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end over HTTP: telemetry attributes the call to the token subject."""
    app = server.http_app(path=MCP_PATH, stateless_http=True)

    def client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        **kwargs: Any,
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
    async with app.router.lifespan_context(app):
        with caplog.at_level(logging.INFO, logger="mcp_server.telemetry"):
            async with Client(transport) as client:
                arguments: dict[str, Any] = {"request": {"run_id": "run-auth-e2e"}}
                with pytest.raises(ToolError, match="not implemented"):
                    await client.call_tool("ledger.list_retrievals", arguments)
    lines = [r.getMessage() for r in caplog.records if "event=mcp_request" in r.getMessage()]
    assert any(f"agent={AGENT_SUBJECT}" in line and "run_id=run-auth-e2e" in line for line in lines)
