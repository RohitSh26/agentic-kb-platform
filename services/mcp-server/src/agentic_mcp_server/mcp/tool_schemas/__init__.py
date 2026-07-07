"""Versioned request/response schemas for MCP Context Broker tools.

Every tool (context.* / graph.* / ledger.* / kb_search) gets a request and
response model here before it is implemented. The human-readable contract of
record is docs/contracts/mcp-tools-contract.md; keep both in sync in the same PR.
"""

from agentic_mcp_server.mcp.tool_schemas.base import MCP_SCHEMA_VERSION, McpModel
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    CreatePackResponse,
    ExpandRequest,
    ExpandResponse,
    OpenEvidenceRequest,
    OpenEvidenceResponse,
    ReadPackRequest,
    ReadPackResponse,
    RequestMoreRequest,
    RequestMoreResponse,
    RequestMoreStatus,
)
from agentic_mcp_server.mcp.tool_schemas.evidence import AgentRole, EvidenceCard, EvidenceLevel
from agentic_mcp_server.mcp.tool_schemas.graph import (
    GetNeighborsRequest,
    GetNeighborsResponse,
    GraphNeighbor,
)
from agentic_mcp_server.mcp.tool_schemas.ledger import (
    ListRetrievalsRequest,
    ListRetrievalsResponse,
    RetrievalEventRecord,
)
from agentic_mcp_server.mcp.tool_schemas.review_draft import (
    GetReviewDraftRequest,
    GetReviewDraftResponse,
    ReviewDraftRecord,
)
from agentic_mcp_server.mcp.tool_schemas.search import (
    ConfidenceTier,
    KbSearchBudget,
    KbSearchHit,
    KbSearchRequest,
    KbSearchResponse,
)
from agentic_mcp_server.mcp.tool_schemas.task_context import (
    AmbiguousCandidate,
    BlastRadius,
    BlastRadiusEntity,
    Convention,
    GetTaskContextRequest,
    GetTaskContextResponse,
    PriorChange,
    ResolutionSource,
    ResolvedScope,
    ScopeEntity,
    TaskContextBudget,
    TaskContextHints,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    RECEIPT_SCHEMA_VERSION,
    ClaimInput,
    ClaimReceipt,
    L0Checks,
    PlatformTrustDecision,
    PlatformTrustRequest,
    VerificationReceipt,
    VerifyAnswerRequest,
)

__all__ = [
    "MCP_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "AgentRole",
    "AmbiguousCandidate",
    "BlastRadius",
    "BlastRadiusEntity",
    "ClaimInput",
    "ClaimReceipt",
    "ConfidenceTier",
    "Convention",
    "CreatePackRequest",
    "CreatePackResponse",
    "EvidenceCard",
    "EvidenceLevel",
    "ExpandRequest",
    "ExpandResponse",
    "GetNeighborsRequest",
    "GetNeighborsResponse",
    "GetReviewDraftRequest",
    "GetReviewDraftResponse",
    "GetTaskContextRequest",
    "GetTaskContextResponse",
    "GraphNeighbor",
    "KbSearchBudget",
    "KbSearchHit",
    "KbSearchRequest",
    "KbSearchResponse",
    "L0Checks",
    "ListRetrievalsRequest",
    "ListRetrievalsResponse",
    "McpModel",
    "OpenEvidenceRequest",
    "OpenEvidenceResponse",
    "PlatformTrustDecision",
    "PlatformTrustRequest",
    "PriorChange",
    "ReadPackRequest",
    "ReadPackResponse",
    "RequestMoreRequest",
    "RequestMoreResponse",
    "RequestMoreStatus",
    "ResolutionSource",
    "ResolvedScope",
    "RetrievalEventRecord",
    "ReviewDraftRecord",
    "ScopeEntity",
    "TaskContextBudget",
    "TaskContextHints",
    "VerificationReceipt",
    "VerifyAnswerRequest",
]
