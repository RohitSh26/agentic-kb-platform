"""Request/response schemas for the ledger.* Context Broker tools.

The retrieval ledger is how budgets and reuse become observable (invariant 3):
every broker call writes a retrieval_event row, and this tool reads them back.
"""

import uuid
from datetime import datetime

from pydantic import Field

from contracts.mcp_schemas.base import McpModel


class ListRetrievalsRequest(McpModel):
    run_id: str = Field(min_length=1)


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
