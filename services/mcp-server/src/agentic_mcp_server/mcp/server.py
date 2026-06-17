"""FastMCP app assembly: auth boundary, telemetry, broker tool surface, health.

The tool surface is registered exclusively from TOOL_SCHEMAS, so a tool cannot
exist at this boundary without a versioned schema.
"""

import logging
import os

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentic_mcp_server.auth import select_verifier
from agentic_mcp_server.auth.client_identity import ClientRegistry, parse_client_registry
from agentic_mcp_server.config import SERVER_NAME, load_config
from agentic_mcp_server.context_broker.budgets import BudgetPolicy, parse_agent_allowances
from agentic_mcp_server.context_broker.dependencies import BrokerDeps, BrokerSettings
from agentic_mcp_server.health import health
from agentic_mcp_server.infrastructure.entailment.client import EntailmentClient
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
    client_registry: ClientRegistry | None = None,
    entailment_client: EntailmentClient | None = None,
) -> FastMCP:
    deps = BrokerDeps(
        session_factory=session_factory,
        search_client=search_client or PostgresKeywordSearchClient(session_factory),
        settings=settings or BrokerSettings(),
        budget_policy=budget_policy or BudgetPolicy(),
        client_registry=client_registry or ClientRegistry(),
        entailment_client=entailment_client,
    )
    server = FastMCP(name=SERVER_NAME, auth=auth, middleware=[TelemetryMiddleware()])

    handlers = make_handlers(deps)
    # Expose a WIRE name that satisfies every MCP client: OpenAI-function-calling clients
    # (Codex/GPT and others) reject tool names that aren't ^[a-z0-9_-]+$, so the dotted
    # canonical name (context.create_pack) is registered as context_create_pack. A
    # description is required by those clients too. The dotted name stays the internal
    # identity (handlers, retrieval_event labels).
    for tool_name, schema in TOOL_SCHEMAS.items():
        server.tool(
            handlers[tool_name],
            name=tool_name.replace(".", "_"),
            description=schema.description,
        )

    @server.custom_route("/health", methods=["GET"])
    async def health_route(request: Request) -> JSONResponse:
        payload = await health(session_factory)
        status_code = 200 if payload["active_kb_version"] is not None else 503
        return JSONResponse(dict(payload), status_code=status_code)

    return server


def create_app() -> FastMCP:
    """Production entrypoint: Entra ID auth + registry-backed health.

    Auth defaults to fail-closed Entra. ``select_verifier`` swaps in the opt-in
    local-dev verifier ONLY when ``MCP_LOCAL_DEV_AUTH`` is set and its guardrails
    hold (ADR-0016); production with the flag unset is unchanged.
    """
    configure_logging()
    config = load_config()
    allowances = parse_agent_allowances(config.agent_allowances_json)
    logger.info("event=agent_allowances_loaded subjects=%d", len(allowances))
    client_registry = parse_client_registry(config.client_registry_json)
    logger.info("event=client_registry_loaded clients=%d", len(client_registry.policies))

    # L3 entailment is OPT-IN: only when MCP_ENABLE_ENTAILMENT is set do we attach an LLM
    # entailment client (built from ENTAIL_LLM_* env, ollama by default). Unset ⇒ the server
    # stays LLM-free and an "L3" request is dropped from verifier_levels_run (provenance-only).
    entailment_client: EntailmentClient | None = None
    if os.environ.get("MCP_ENABLE_ENTAILMENT", "").strip().lower() in {"1", "true", "yes", "on"}:
        from agentic_mcp_server.infrastructure.entailment.ollama_client import (
            OllamaEntailmentClient,
        )

        entailment_client = OllamaEntailmentClient.from_env()
        logger.info("event=entailment_enabled")

    engine = create_engine(config.database_url)
    return build_server(
        auth=select_verifier(config),
        session_factory=create_session_factory(engine),
        budget_policy=BudgetPolicy(allowances=allowances),
        client_registry=client_registry,
        entailment_client=entailment_client,
    )
