"""FastMCP app assembly: auth boundary, telemetry, stub tool surface, health.

The tool surface is registered exclusively from contracts' TOOL_SCHEMAS, so a
tool cannot exist at this boundary without a versioned schema. Every tool is a
stub until the Context Broker lands (PR-10): requests are validated against
the contract, then rejected with "not implemented".
"""

from collections.abc import Callable, Coroutine
from typing import Any, NoReturn

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from contracts.mcp_schemas import TOOL_SCHEMAS, McpModel, ToolSchema
from db.session import create_engine, create_session_factory
from mcp_server.auth import build_entra_verifier
from mcp_server.config import SERVER_NAME, load_config
from mcp_server.health import health
from mcp_server.telemetry import TelemetryMiddleware

StubFn = Callable[[McpModel], Coroutine[Any, Any, McpModel]]


def _make_stub(tool_name: str, schema: ToolSchema) -> StubFn:
    async def stub(request: McpModel) -> NoReturn:
        raise ToolError(f"{tool_name} is not implemented yet; the Context Broker arrives in PR-10")

    stub.__name__ = tool_name.replace(".", "_")
    # fastmcp derives the input/output schemas from these annotations, so the
    # stub enforces the versioned contract even before the broker exists
    stub.__annotations__ = {"request": schema.request, "return": schema.response}
    return stub


def build_server(
    *,
    auth: AuthProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> FastMCP:
    server = FastMCP(name=SERVER_NAME, auth=auth, middleware=[TelemetryMiddleware()])

    for tool_name, schema in TOOL_SCHEMAS.items():
        server.tool(_make_stub(tool_name, schema), name=tool_name)

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
