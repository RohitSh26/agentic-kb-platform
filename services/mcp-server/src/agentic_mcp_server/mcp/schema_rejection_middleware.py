"""MCP-boundary ledger completeness for schema-rejected tool calls.

FastMCP validates a tool call's arguments against its registered pydantic
request schema BEFORE any handler in ``tool_handlers.py`` runs: ``FunctionTool.run``
calls ``type_adapter.validate_python(arguments)``, which validates AND invokes the
handler in one step. A malformed call (a real host's most likely failure mode —
docs/reports/host-integration-2026-07-06.md finding 4) therefore never reaches
``_ledgered`` and, left alone, writes no ``retrieval_event`` row at all —
violating the "exactly one row per call" ledger-completeness guarantee
(mcp-tools-contract.md, commit 346c2d2) for exactly that call class.

This middleware is the MCP-boundary counterpart to ``_ledgered``: it wraps the
WHOLE call (argument validation included, since fastmcp's own middleware chain
sits outside ``FunctionTool.run``) and writes one ``error`` row the instant a
``pydantic.ValidationError`` surfaces, then re-raises the SAME exception
unchanged — the host still needs the verbatim schema feedback for its retry
loop. Fail-soft like ``_write_unexpected_error``: a broken ledger write is
logged and swallowed, never masking the validation error. No handler ever ran,
so there is nothing to refund — no budget charge was ever made.

Only ``pydantic.ValidationError`` is handled here. Every other exception either
already reached a handler (and was ledgered exactly once by ``_ledgered``) or is
an out-of-scope MCP-level rejection (unknown tool, auth) that this middleware
must not double-ledger. No handler in this codebase raises a bare
``pydantic.ValidationError`` internally (grepped: none do); if one ever did, it
would need its own ``LedgeredToolError``-style marker to stay exactly-once here,
same as anticipated-failure call sites already do for ``_ledgered``.
"""

import contextlib
import logging
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams
from pydantic import ValidationError as PydanticValidationError

from agentic_mcp_server.context_broker.dependencies import BrokerDeps, current_requester
from agentic_mcp_server.context_broker.error_ledger import UNRESOLVED, write_error_event
from agentic_mcp_server.mcp.tool_registry import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

# fastmcp registers each tool under a "." -> "_" wire name (mcp/server.py) because
# OpenAI-function-calling clients reject dotted names. The ledger's tool_name
# column stores the canonical dotted name everywhere else (see _ledgered), so map
# the wire name back for parity. Tools with no dot (kb_search, get_task_context)
# round-trip unchanged.
_CANONICAL_TOOL_NAMES: dict[str, str] = {
    tool_name.replace(".", "_"): tool_name for tool_name in TOOL_SCHEMAS
}


def _canonical_tool_name(wire_name: str) -> str:
    return _CANONICAL_TOOL_NAMES.get(wire_name, wire_name)


def _validation_summary(exc: PydanticValidationError) -> list[dict[str, str]]:
    """Terse, safe validation summary: field path + error type + message.

    NEVER the raw argument values a host sent — pydantic's ``errors()`` carries
    an ``input`` key with exactly that, so it is deliberately excluded here.
    """
    return [
        {
            "loc": ".".join(str(part) for part in error["loc"]),
            "type": error["type"],
            "msg": error["msg"],
        }
        for error in exc.errors(include_url=False)
    ]


class SchemaRejectionLedgerMiddleware(Middleware):
    """Writes one ``error`` retrieval_event for calls fastmcp rejects at the
    schema boundary (before any handler or budget charge), then re-raises the
    validation error unchanged so the client still sees it."""

    def __init__(self, deps: BrokerDeps) -> None:
        self._deps = deps

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        try:
            return await call_next(context)
        except PydanticValidationError as exc:
            tool_name = _canonical_tool_name(context.message.name)
            subject = UNRESOLVED
            with contextlib.suppress(Exception):
                subject = current_requester().subject
            try:
                await write_error_event(
                    self._deps,
                    tool_name=tool_name,
                    subject=subject,
                    details={
                        "exception_type": type(exc).__name__,
                        "validation_errors": _validation_summary(exc),
                    },
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
            raise
