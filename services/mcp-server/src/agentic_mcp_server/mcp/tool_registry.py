"""Authoritative tool-name → request/response schema registry.

The MCP server registers its tool surface from this table, so a tool cannot
exist at the boundary without a versioned contract (schema before code; see
docs/contracts/mcp-tools-contract.md).
"""

from dataclasses import dataclass

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.change import (
    ChangeContextRequest,
    ChangeContextResponse,
)
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
)
from agentic_mcp_server.mcp.tool_schemas.graph import GetNeighborsRequest, GetNeighborsResponse
from agentic_mcp_server.mcp.tool_schemas.ledger import (
    ListRetrievalsRequest,
    ListRetrievalsResponse,
)
from agentic_mcp_server.mcp.tool_schemas.search import KbSearchRequest, KbSearchResponse
from agentic_mcp_server.mcp.tool_schemas.task_context import (
    GetTaskContextRequest,
    GetTaskContextResponse,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    PlatformTrustDecision,
    PlatformTrustRequest,
    VerificationReceipt,
    VerifyAnswerRequest,
)


@dataclass(frozen=True)
class ToolSchema:
    request: type[McpModel]
    response: type[McpModel]
    # Shown to the model so it knows when to call the tool. MCP clients that bridge to
    # OpenAI function-calling REQUIRE a description, and reject a tool without one.
    description: str


# Canonical (dotted) tool names. The WIRE name the MCP server exposes is derived from
# these by replacing '.' with '_' (mcp/server.py), because OpenAI-function-calling clients
# (Codex/GPT, and others) reject names that don't match ^[a-z0-9_-]+$. The dotted form
# stays the internal identity (handlers, retrieval_event audit labels, contracts).
TOOL_SCHEMAS: dict[str, ToolSchema] = {
    "context.create_pack": ToolSchema(
        CreatePackRequest,
        CreatePackResponse,
        "Retrieve once and return a budgeted Evidence Pack of L0/L1 cards (by handle, not "
        "raw text) for a task. Start here.",
    ),
    "context.read_pack": ToolSchema(
        ReadPackRequest,
        ReadPackResponse,
        "Return the run's existing Evidence Pack cards (free, reused; no new retrieval).",
    ),
    "context.request_more": ToolSchema(
        RequestMoreRequest,
        RequestMoreResponse,
        "Request more context with a justified question (question, why_needed, "
        "decision_needed, already_checked, max_tokens). A bare query is rejected.",
    ),
    "context.open_evidence": ToolSchema(
        OpenEvidenceRequest,
        OpenEvidenceResponse,
        "Open one evidence card to its raw source text (L2) by handle, metered against the "
        "pack budget.",
    ),
    "context.expand": ToolSchema(
        ExpandRequest,
        ExpandResponse,
        "Walk the knowledge graph from seed cards (trust-tiered, ACL-filtered) to pull the "
        "connected neighbourhood into the pack in one call.",
    ),
    "graph.get_neighbors": ToolSchema(
        GetNeighborsRequest,
        GetNeighborsResponse,
        "Return a node's neighbours from the Postgres knowledge graph (EXTRACTED edges by "
        "default).",
    ),
    "ledger.list_retrievals": ToolSchema(
        ListRetrievalsRequest,
        ListRetrievalsResponse,
        "List the retrieval_event audit rows for a run — what the broker did on your behalf.",
    ),
    "context.verify_answer": ToolSchema(
        VerifyAnswerRequest,
        VerificationReceipt,
        "Verify an answer's claims against their cited evidence (L0 provenance checks) and "
        "return a receipt.",
    ),
    "context.platform_trust": ToolSchema(
        PlatformTrustRequest,
        PlatformTrustDecision,
        "Return the platform trust decision for an answer (whether it carries a valid "
        "verification receipt).",
    ),
    "context.create_change_pack": ToolSchema(
        ChangeContextRequest,
        ChangeContextResponse,
        "For a code-change task, return the small set of files to edit: the target file(s), "
        "the test file(s), and the top dependency file(s) — each with a reason, numeric "
        "confidence, and token estimate. Use this to gather BUILD context instead of grep.",
    ),
    # ADR-0025: the KB-first simple path. Already '_'-shaped, so the wire name is identical.
    "kb_search": ToolSchema(
        KbSearchRequest,
        KbSearchResponse,
        "Search the knowledge base (code + docs + tickets) for the right files and answers. "
        "Prefer this FIRST to find where things are; cite the source_uri of results you use, "
        "and do not re-read files it already answered. Budgeted — a per-task call + token cap "
        "is enforced server-side; each response reports budget_remaining.",
    ),
    # ADR-0030 §2 (PR-39): one call for a coding TASK. Already '_'-shaped like kb_search.
    "get_task_context": ToolSchema(
        GetTaskContextRequest,
        GetTaskContextResponse,
        "For a coding task, get everything at once in ONE call: the resolved files/symbols in "
        "scope, their blast radius (callers, callees, tests), the conventions that apply, and "
        "similar prior changes — each item tiered by confidence and citing its source. Call "
        "this FIRST for any change task instead of exploring files; ambiguous scope comes back "
        "as candidates + open questions, never a guess.",
    ),
}
