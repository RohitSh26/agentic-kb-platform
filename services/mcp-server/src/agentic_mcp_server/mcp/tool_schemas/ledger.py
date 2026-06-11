"""Request/response schemas for the ledger.* Context Broker tools.

The retrieval ledger is how budgets and reuse become observable (invariant 3):
every broker call writes a retrieval_event row, and this tool reads them back.
"""

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.context import RUN_ID_PATTERN


class ListRetrievalsRequest(McpModel):
    run_id: str = Field(pattern=RUN_ID_PATTERN)

    @field_validator("run_id")
    @classmethod
    def _reject_non_run_sentinel(cls, value: str) -> str:
        # "-" aggregates every subject's non-run-scoped activity (graph
        # lookups, unresolved errors); it is operator-only, never listable
        if value == "-":
            raise ValueError("run_id '-' is the non-run sentinel and cannot be listed")
        return value


class RetrievalEventRecord(McpModel):
    event_id: uuid.UUID
    run_id: str
    kb_version: str
    agent_name: str
    tool: str
    status: str
    cache_hit: bool
    tokens_returned: int = Field(ge=0)
    evidence_ids: list[str]
    created_at: datetime


class ListRetrievalsResponse(McpModel):
    run_id: str
    events: list[RetrievalEventRecord]
