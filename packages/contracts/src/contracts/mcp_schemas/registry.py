"""Authoritative tool-name → request/response schema registry.

The MCP server registers its tool surface from this table, so a tool cannot
exist at the boundary without a versioned contract (mcp-tools rule:
schema before code).
"""

from dataclasses import dataclass

from contracts.mcp_schemas.base import McpModel
from contracts.mcp_schemas.context import (
    CreatePackRequest,
    CreatePackResponse,
    OpenEvidenceRequest,
    OpenEvidenceResponse,
    ReadPackRequest,
    ReadPackResponse,
    RequestMoreRequest,
    RequestMoreResponse,
)
from contracts.mcp_schemas.graph import GetNeighborsRequest, GetNeighborsResponse
from contracts.mcp_schemas.ledger import ListRetrievalsRequest, ListRetrievalsResponse


@dataclass(frozen=True)
class ToolSchema:
    request: type[McpModel]
    response: type[McpModel]


TOOL_SCHEMAS: dict[str, ToolSchema] = {
    "context.create_pack": ToolSchema(CreatePackRequest, CreatePackResponse),
    "context.read_pack": ToolSchema(ReadPackRequest, ReadPackResponse),
    "context.request_more": ToolSchema(RequestMoreRequest, RequestMoreResponse),
    "context.open_evidence": ToolSchema(OpenEvidenceRequest, OpenEvidenceResponse),
    "graph.get_neighbors": ToolSchema(GetNeighborsRequest, GetNeighborsResponse),
    "ledger.list_retrievals": ToolSchema(ListRetrievalsRequest, ListRetrievalsResponse),
}
