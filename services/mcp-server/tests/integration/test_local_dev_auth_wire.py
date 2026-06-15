"""Over-the-wire: the local-dev verifier authorizes a tool call as the dev identity.

Goes through the real HTTP app (httpx ASGI transport, in process) — the same
boundary fastmcp enforces auth at — so this proves the opt-in dev verifier mints
an identity that flows through the NORMAL authorization path: the telemetry
middleware attributes the call to the dev subject, exactly as the Entra path does.
"""

import logging
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp_test_support import MCP_PATH, make_session_factory

from agentic_mcp_server.auth.local_dev import LocalDevIdentity, LocalDevTokenVerifier
from agentic_mcp_server.mcp.server import build_server

DEV_SUBJECT = "local-dev"


async def test_dev_verifier_authorizes_tool_call_as_dev_subject(
    caplog: pytest.LogCaptureFixture,
) -> None:
    identity = LocalDevIdentity(subject=DEV_SUBJECT, teams=("team-a",), client_id="local-dev")
    server = build_server(
        auth=LocalDevTokenVerifier(identity),
        session_factory=make_session_factory(),
    )
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

    # Any bearer string is accepted when dev-auth is active (developer convenience).
    transport = StreamableHttpTransport(
        url=f"http://testserver{MCP_PATH}",
        headers={"Authorization": "Bearer any-local-dev-bearer"},
        httpx_client_factory=client_factory,
    )
    async with app.router.lifespan_context(app):
        with caplog.at_level(logging.INFO, logger="agentic_mcp_server.telemetry.middleware"):
            async with Client(transport) as client:
                # Extra field fails contract validation deterministically (no DB);
                # the middleware logs identity before validation runs.
                arguments: dict[str, Any] = {
                    "request": {"run_id": "run-dev-auth", "unexpected_field": True}
                }
                with pytest.raises(ToolError):
                    await client.call_tool("ledger.list_retrievals", arguments)

    lines = [r.getMessage() for r in caplog.records if "event=mcp_request" in r.getMessage()]
    assert any(f"agent={DEV_SUBJECT}" in line and "run_id=run-dev-auth" in line for line in lines)
