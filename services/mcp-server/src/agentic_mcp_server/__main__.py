"""Container / `python -m` entrypoint: serve the Context Broker over streamable HTTP.

This is the single boot path the Dockerfile CMD and a bare `uv run python -m
agentic_mcp_server` both use, so a developer on a separate machine starts the
*same* app `server.create_app()` builds — no Docker required. Host/port/path are
read from the environment so a dev can bind a free port without code changes;
they affect the transport only, never the broker (auth, budgets, ACLs, evidence,
ledger are untouched). Auth stays fail-closed Entra (invariant 6): there is no
auth-off switch here, and DATABASE_URL / MCP_ENTRA_* are still required by
``load_config`` inside ``create_app``.
"""

import os

from agentic_mcp_server.mcp.server import create_app


def main() -> None:
    create_app().run(
        transport="http",
        host=os.environ.get("MCP_HTTP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_HTTP_PORT", "8000")),
        path=os.environ.get("MCP_HTTP_PATH", "/mcp/"),
    )


if __name__ == "__main__":
    main()
