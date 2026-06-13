"""FastMCP app assembly: auth boundary, telemetry, broker tool surface, health.

The tool surface is registered exclusively from TOOL_SCHEMAS, so a tool cannot
exist at this boundary without a versioned schema.
"""

import logging

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentic_mcp_server.auth import build_entra_verifier
from agentic_mcp_server.config import SERVER_NAME, load_config
from agentic_mcp_server.context_broker.budgets import BudgetPolicy, parse_agent_allowances
from agentic_mcp_server.context_broker.dependencies import BrokerDeps, BrokerSettings
from agentic_mcp_server.health import health
from agentic_mcp_server.infrastructure.postgres.keyword_search import PostgresKeywordSearchClient
from agentic_mcp_server.infrastructure.postgres.session import (
    create_engine,
    create_session_factory,
)
from agentic_mcp_server.infrastructure.search.search_client import SearchClient
from agentic_mcp_server.mcp.tool_handlers import make_handlers
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.structured_logging import configure_logging
from agentic_mcp_server.telemetry import TelemetryMiddleware

logger = logging.getLogger(__name__)


def build_server(
    *,
    auth: AuthProvider,
    session_factory: async_sessionmaker[AsyncSession],
    search_client: SearchClient | None = None,
    settings: BrokerSettings | None = None,
    budget_policy: BudgetPolicy | None = None,
) -> FastMCP:
    deps = BrokerDeps(
        session_factory=session_factory,
        search_client=search_client or PostgresKeywordSearchClient(session_factory),
        settings=settings or BrokerSettings(),
        budget_policy=budget_policy or BudgetPolicy(),
    )
    server = FastMCP(name=SERVER_NAME, auth=auth, middleware=[TelemetryMiddleware()])

    handlers = make_handlers(deps)
    for tool_name in TOOL_SCHEMAS:
        server.tool(handlers[tool_name], name=tool_name)

    @server.custom_route("/health", methods=["GET"])
    async def health_route(request: Request) -> JSONResponse:
        payload = await health(session_factory)
        status_code = 200 if payload["active_kb_version"] is not None else 503
        return JSONResponse(dict(payload), status_code=status_code)

    return server


def create_app() -> FastMCP:
    """Production entrypoint: Entra ID auth + registry-backed health."""
    configure_logging()
    config = load_config()
    allowances = parse_agent_allowances(config.agent_allowances_json)
    logger.info("event=agent_allowances_loaded subjects=%d", len(allowances))
    engine = create_engine(config.database_url)
    return build_server(
        auth=build_entra_verifier(config),
        session_factory=create_session_factory(engine),
        budget_policy=BudgetPolicy(allowances=allowances),
    )
