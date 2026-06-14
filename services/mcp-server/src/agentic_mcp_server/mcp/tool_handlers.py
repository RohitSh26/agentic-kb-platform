"""Context Broker handlers for the registered tool surface.

Each handler binds a tool name from TOOL_SCHEMAS to its broker implementation.
Identity is resolved per call from the authenticated session (never from
request fields), and fastmcp validates I/O against the versioned schemas via
the annotations set here.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from agentic_mcp_server.context_broker import (
    evidence,
    graph,
    ledger,
    pack,
    request_more,
    verify,
)
from agentic_mcp_server.context_broker.dependencies import BrokerDeps, current_requester
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    OpenEvidenceRequest,
    ReadPackRequest,
    RequestMoreRequest,
)
from agentic_mcp_server.mcp.tool_schemas.graph import GetNeighborsRequest
from agentic_mcp_server.mcp.tool_schemas.ledger import ListRetrievalsRequest
from agentic_mcp_server.mcp.tool_schemas.verification import VerifyAnswerRequest

HandlerFn = Callable[..., Coroutine[Any, Any, McpModel]]


def make_handlers(deps: BrokerDeps) -> dict[str, HandlerFn]:
    async def create_pack(request: CreatePackRequest) -> McpModel:
        return await pack.create_pack(deps, request, current_requester())

    async def read_pack(request: ReadPackRequest) -> McpModel:
        return await pack.read_pack(deps, request, current_requester())

    async def request_more_handler(request: RequestMoreRequest) -> McpModel:
        return await request_more.request_more(deps, request, current_requester())

    async def open_evidence(request: OpenEvidenceRequest) -> McpModel:
        return await evidence.open_evidence(deps, request, current_requester())

    async def get_neighbors(request: GetNeighborsRequest) -> McpModel:
        return await graph.get_neighbors(deps, request, current_requester())

    async def list_retrievals(request: ListRetrievalsRequest) -> McpModel:
        return await ledger.list_retrievals(deps, request, current_requester())

    async def verify_answer(request: VerifyAnswerRequest) -> McpModel:
        return await verify.verify_answer(deps, request, current_requester())

    handlers: dict[str, HandlerFn] = {
        "context.create_pack": create_pack,
        "context.read_pack": read_pack,
        "context.request_more": request_more_handler,
        "context.open_evidence": open_evidence,
        "graph.get_neighbors": get_neighbors,
        "ledger.list_retrievals": list_retrievals,
        "context.verify_answer": verify_answer,
    }
    for tool_name, handler in handlers.items():
        schema = TOOL_SCHEMAS[tool_name]
        handler.__name__ = tool_name.replace(".", "_")
        # fastmcp derives input/output schemas from these annotations, keeping
        # the wire contract pinned to the versioned schema registry
        handler.__annotations__ = {"request": schema.request, "return": schema.response}
    return handlers
