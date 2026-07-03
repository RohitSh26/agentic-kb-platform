"""Context Broker handlers for the registered tool surface.

Each handler binds a tool name from TOOL_SCHEMAS to its broker implementation.
Identity is resolved per call from the authenticated session (never from
request fields), and fastmcp validates I/O against the versioned schemas via
the annotations set here.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.auth.scopes import client_may_call
from agentic_mcp_server.context_broker import (
    change_context,
    evidence,
    graph,
    ledger,
    pack,
    request_more,
    verify,
)
from agentic_mcp_server.context_broker import (
    expand as expand_mod,
)
from agentic_mcp_server.context_broker import (
    kb_search as kb_search_mod,
)
from agentic_mcp_server.context_broker import (
    task_context as task_context_mod,
)
from agentic_mcp_server.context_broker.dependencies import (
    BrokerDeps,
    current_client_identity,
    current_requester,
    current_session_key,
)
from agentic_mcp_server.context_broker.platform_trust import evaluate_platform_trust
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS
from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.change import ChangeContextRequest
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    ExpandRequest,
    OpenEvidenceRequest,
    ReadPackRequest,
    RequestMoreRequest,
)
from agentic_mcp_server.mcp.tool_schemas.graph import GetNeighborsRequest
from agentic_mcp_server.mcp.tool_schemas.ledger import ListRetrievalsRequest
from agentic_mcp_server.mcp.tool_schemas.search import KbSearchRequest
from agentic_mcp_server.mcp.tool_schemas.task_context import GetTaskContextRequest
from agentic_mcp_server.mcp.tool_schemas.verification import (
    PlatformTrustRequest,
    VerifyAnswerRequest,
)

HandlerFn = Callable[..., Coroutine[Any, Any, McpModel]]


def make_handlers(deps: BrokerDeps) -> dict[str, HandlerFn]:
    def _client(tool_name: str) -> ClientIdentity:
        # Resolve the client/app identity (alongside the user Requester) and enforce
        # its scope grant ADDITIVELY — a registered client lacking the tool's scope is
        # denied here, before the tool runs (and before the user ACL filter the tool
        # then applies). Unregistered clients are never scope-gated (opt-in only).
        client = current_client_identity(deps.client_registry)
        if not client_may_call(client, tool_name):
            raise ToolError(f"client not authorized for {tool_name} (missing scope)")
        return client

    async def create_pack(request: CreatePackRequest) -> McpModel:
        _client("context.create_pack")
        return await pack.create_pack(deps, request, current_requester())

    async def read_pack(request: ReadPackRequest) -> McpModel:
        _client("context.read_pack")
        return await pack.read_pack(deps, request, current_requester())

    async def request_more_handler(request: RequestMoreRequest) -> McpModel:
        _client("context.request_more")
        return await request_more.request_more(deps, request, current_requester())

    async def open_evidence(request: OpenEvidenceRequest) -> McpModel:
        _client("context.open_evidence")
        return await evidence.open_evidence(deps, request, current_requester())

    async def expand(request: ExpandRequest) -> McpModel:
        _client("context.expand")
        return await expand_mod.expand(deps, request, current_requester())

    async def get_neighbors(request: GetNeighborsRequest) -> McpModel:
        _client("graph.get_neighbors")
        return await graph.get_neighbors(deps, request, current_requester())

    async def list_retrievals(request: ListRetrievalsRequest) -> McpModel:
        _client("ledger.list_retrievals")
        return await ledger.list_retrievals(deps, request, current_requester())

    async def verify_answer(request: VerifyAnswerRequest) -> McpModel:
        client = _client("context.verify_answer")
        # Stamp the validated client into the receipt (binds + scopes it).
        return await verify.verify_answer(deps, request, current_requester(), client)

    async def create_change_pack(request: ChangeContextRequest) -> McpModel:
        _client("context.create_change_pack")
        return await change_context.create_change_pack(deps, request, current_requester())

    async def kb_search(request: KbSearchRequest) -> McpModel:
        _client("kb_search")
        # The budget window binds to (MCP session, authenticated subject) — both
        # resolved server-side, never from request fields (unspoofable, like budgets).
        return await kb_search_mod.kb_search(
            deps, request, current_requester(), session_key=current_session_key()
        )

    async def get_task_context(request: GetTaskContextRequest) -> McpModel:
        _client("get_task_context")
        # Same read-capability class as kb_search: identity binds to the
        # authenticated session; the response cap is server-side (never a
        # request escape hatch), so no per-window budget key is needed here.
        return await task_context_mod.get_task_context(deps, request, current_requester())

    async def platform_trust(request: PlatformTrustRequest) -> McpModel:
        # Official-client gate: trusted ONLY for a verification_required client with a
        # valid, client-matched, passing receipt; structured denial otherwise; a
        # non-opted-in client gets not_required (unchanged). Composes with the ACL +
        # trust filters the retrieval tools already enforced.
        client = _client("context.platform_trust")
        return evaluate_platform_trust(
            client, request.receipt, signing_key_env=deps.settings.signing_key_env
        )

    handlers: dict[str, HandlerFn] = {
        "context.create_pack": create_pack,
        "context.read_pack": read_pack,
        "context.request_more": request_more_handler,
        "context.open_evidence": open_evidence,
        "context.expand": expand,
        "graph.get_neighbors": get_neighbors,
        "ledger.list_retrievals": list_retrievals,
        "context.verify_answer": verify_answer,
        "context.platform_trust": platform_trust,
        "context.create_change_pack": create_change_pack,
        "kb_search": kb_search,
        "get_task_context": get_task_context,
    }
    for tool_name, handler in handlers.items():
        schema = TOOL_SCHEMAS[tool_name]
        handler.__name__ = tool_name.replace(".", "_")
        # fastmcp derives input/output schemas from these annotations, keeping
        # the wire contract pinned to the versioned schema registry
        handler.__annotations__ = {"request": schema.request, "return": schema.response}
    return handlers
