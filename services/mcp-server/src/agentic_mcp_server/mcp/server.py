"""FastMCP app assembly: auth boundary, telemetry, stub tool surface, health.

The tool surface is registered exclusively from TOOL_SCHEMAS, so a tool cannot
exist at this boundary without a versioned schema.
"""

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentic_mcp_server.auth import build_entra_verifier
from agentic_mcp_server.config import SERVER_NAME, load_config
from agentic_mcp_server.health import health
from agentic_mcp_server.infrastructure.postgres.session import (
    create_engine,
    create_session_factory,
)
from agentic_mcp_server.mcp.tool_handlers import make_stub
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.telemetry import TelemetryMiddleware


def build_server(
    *,
    auth: AuthProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> FastMCP:
    server = FastMCP(name=SERVER_NAME, auth=auth, middleware=[TelemetryMiddleware()])

    for tool_name, schema in TOOL_SCHEMAS.items():
        server.tool(make_stub(tool_name, schema), name=tool_name)

    @server.custom_route("/health", methods=["GET"])
    async def health_route(request: Request) -> JSONResponse:
        payload = await health(session_factory)
        status_code = 200 if payload["active_kb_version"] is not None else 503
        return JSONResponse(dict(payload), status_code=status_code)

    return server


def create_app() -> FastMCP:
    """Production entrypoint: Entra ID auth + registry-backed health."""
    config = load_config()
    engine = create_engine(config.database_url)
    return build_server(
        auth=build_entra_verifier(config),
        session_factory=create_session_factory(engine),
    )
