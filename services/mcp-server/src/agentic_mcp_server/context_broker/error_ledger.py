"""Ledger rows for failed tool calls.

Every broker call writes a retrieval_event — including the ones that fail
before reaching a pack or the registry (unknown handles, no active
kb_version). Those are audited with the ledger-only status 'error'; "-" marks
run/kb_version values the broker could not resolve.

``LedgeredToolError`` is the marker every anticipated-failure call site raises
right after calling ``write_error_event`` here. The uniform tool wrapper in
``mcp/tool_handlers.py`` catches it and re-raises without writing a second
row — the single idiom that keeps the ledger at exactly one row per call
without every module needing to know about the wrapper.
"""

import uuid
from typing import Any

from fastmcp.exceptions import ToolError

from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)

UNRESOLVED = "-"


class LedgeredToolError(ToolError):
    """A ToolError whose retrieval_event row was already written.

    Raise this (never a bare ``ToolError``) immediately after ``write_error_event``
    at an anticipated-failure call site. The tool_handlers.py wrapper recognizes
    it and skips its own error-ledger write, so the failure is ledgered exactly
    once no matter how many broker layers see the exception on its way out.
    """


async def write_error_event(
    deps: BrokerDeps,
    *,
    tool_name: str,
    subject: str,
    run_id: str = UNRESOLVED,
    kb_version: str = UNRESOLVED,
    context_pack_id: uuid.UUID | None = None,
    query_text: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=run_id,
                agent_name=subject,
                tool_name=tool_name,
                status="error",
                kb_version=kb_version,
                context_pack_id=context_pack_id,
                query_text=query_text,
                details=details,
            ),
        )
