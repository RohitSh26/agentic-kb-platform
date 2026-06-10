"""Request/response schemas for the ledger.* Context Broker tools.

The retrieval ledger is how budgets and reuse become observable (invariant 3):
every broker call writes a retrieval_event row, and this tool reads them back.
"""

import uuid
from datetime import datetime

from pydantic import Field

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.context import RUN_ID_PATTERN


class ListRetrievalsRequest(McpModel):
    run_id: str = Field(pattern=RUN_ID_PATTERN)


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
