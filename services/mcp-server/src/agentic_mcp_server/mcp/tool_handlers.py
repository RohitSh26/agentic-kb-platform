"""Context Broker handlers for the registered tool surface.

Each handler binds a tool name from TOOL_SCHEMAS to its broker implementation.
Identity is resolved per call from the authenticated session (never from
request fields), and fastmcp validates I/O against the versioned schemas via
the annotations set here.

Every handler is also wrapped by ``_ledgered`` below: the place responsible for
the ledger's completeness guarantee for anything that reaches a handler (an
unexpected exception must still produce exactly one ``retrieval_event`` row and
must still reach the MCP client, never be swallowed here). A call whose
arguments fail fastmcp's own schema validation never reaches a handler at all
(fastmcp validates before invoking the registered callable); that call class is
ledgered instead by ``SchemaRejectionLedgerMiddleware``
(``mcp/schema_rejection_middleware.py``), the MCP-boundary counterpart to this
wrapper.
"""

import contextlib
import logging
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
from agentic_mcp_server.context_broker.error_ledger import (
    UNRESOLVED,
    LedgeredToolError,
    write_error_event,
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

logger = logging.getLogger(__name__)


async def _write_unexpected_error(deps: BrokerDeps, tool_name: str, exc: Exception) -> None:
    """Best-effort single ledger row for an exception no handler already ledgered.

    Never lets a broken ledger mask the original failure: if the write itself
    raises (DB fully down), that is logged with structured fields and
    swallowed here — the caller re-raises ``exc`` regardless.
    """
    subject = UNRESOLVED
    with contextlib.suppress(Exception):
        subject = current_requester().subject
    try:
        await write_error_event(
            deps,
            tool_name=tool_name,
            subject=subject,
            details={"exception_type": type(exc).__name__},
        )
    except Exception as ledger_exc:
        logger.error(
            "event=error_ledger_write_failed tool_name=%s exception_type=%s "
            "ledger_exception_type=%s",
            tool_name,
            type(exc).__name__,
            type(ledger_exc).__name__,
            exc_info=True,
        )


def _ledgered(deps: BrokerDeps, tool_name: str, handler: HandlerFn) -> HandlerFn:
    """Uniform error-ledger wrapper: the ledger is complete by construction.

    ``LedgeredToolError`` marks a call site that already wrote its own
    retrieval_event (error_ledger.write_error_event) immediately before
    raising — those pass through untouched so the row is never doubled. Any
    other exception is unexpected: this is the sole remaining place that owes
    the ledger a row for it. Either way the exception always propagates —
    a tool failure is surfaced to the MCP client, never swallowed, so the
    host can fall back to native tools.
    """

    async def _wrapped(request: McpModel) -> McpModel:
        try:
            return await handler(request)
        except LedgeredToolError:
            raise
        except Exception as exc:
            await _write_unexpected_error(deps, tool_name, exc)
            raise

    return _wrapped


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

    raw_handlers: dict[str, HandlerFn] = {
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
    handlers: dict[str, HandlerFn] = {}
    for tool_name, handler in raw_handlers.items():
        schema = TOOL_SCHEMAS[tool_name]
        # Every tool call goes through the uniform error-ledger wrapper — one
        # idiom for the whole surface, applied here rather than in each broker
        # module (see _ledgered's docstring).
        wrapped = _ledgered(deps, tool_name, handler)
        wrapped.__name__ = tool_name.replace(".", "_")
        # fastmcp derives input/output schemas from these annotations, keeping
        # the wire contract pinned to the versioned schema registry
        wrapped.__annotations__ = {"request": schema.request, "return": schema.response}
        handlers[tool_name] = wrapped
    return handlers
