"""Request/response schemas for the context.* Context Broker tools.

Schema-first per docs/contracts/mcp-tools-contract.md: these models are the
contract the PR-10 broker implements against; until then they are registered
on stub tools. extra="forbid" plus required justification fields is what
rejects a bare {"query": "..."} at the schema layer — the broker never sees it.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from agentic_mcp_server.mcp.tool_schemas.base import McpModel
from agentic_mcp_server.mcp.tool_schemas.evidence import (
    AgentRole,
    AuthorizationDecision,
    EvidenceCard,
)

# run ids land verbatim in key=value audit logs; the charset constraint makes
# log-line forgery (spaces/newlines smuggling fake fields) impossible
RUN_ID_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"


# Query intent (PR-33, golden-query-evals.md `intent`): drives the broker's
# transparent temporal re-weighting (current code first for `how`, cards/PRs/ADRs
# for `why`). Optional + additive — omitting it ⇒ neutral (pre-PR-33) ranking. It
# is a RANKING hint only; it never changes ACL, membership, or the L0 verifier.
QueryIntent = Literal[
    "how_does_x_work",
    "why_was_x_changed",
    "who_owns_x",
    "what_calls_x",
]


class CreatePackRequest(McpModel):
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    task: str = Field(min_length=1)
    approved_context_plan: str = Field(min_length=1)
    retrieval_profile: str = Field(min_length=1)
    budget_tokens: int = Field(ge=1)
    intent: QueryIntent | None = None


class CreatePackResponse(McpModel):
    context_pack_id: str = Field(min_length=1)
    kb_version: str = Field(min_length=1)
    summary: str
    evidence_cards: list[EvidenceCard]
    open_questions: list[str]
    budget_used_tokens: int = Field(ge=0)
    authorization: AuthorizationDecision


class ReadPackRequest(McpModel):
    """Role-specific view of a pack.

    `role` selects a *view* only and is free-form — adopting teams name their
    own roles. Authorization, ledger attribution, and per-agent budget identity
    always come from the authenticated MCP session, never from request fields —
    a spoofed role must not grant another role's access.
    """

    context_pack_id: str = Field(min_length=1)
    role: AgentRole


class ReadPackResponse(McpModel):
    context_pack_id: str = Field(min_length=1)
    kb_version: str = Field(min_length=1)
    role: AgentRole
    summary: str
    evidence_cards: list[EvidenceCard]
    open_questions: list[str]
    budget_remaining_tokens: int = Field(ge=0)
    authorization: AuthorizationDecision


RequestMoreStatus = Literal["reused", "approved", "denied", "needs_human_approval"]


class RequestMoreRequest(McpModel):
    """Justified follow-up retrieval.

    `agent_name` is correlation metadata only; budget and authorization
    identity bind to the authenticated MCP session, exactly as with
    ReadPackRequest.role.
    """

    context_pack_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    question: str = Field(min_length=1)
    why_needed: str = Field(min_length=1)
    decision_needed: str = Field(min_length=1)
    already_checked_evidence_ids: list[str]
    max_tokens: int = Field(ge=1)


class RequestMoreResponse(McpModel):
    status: RequestMoreStatus
    reused_evidence_ids: list[str]
    new_evidence_cards: list[EvidenceCard]
    tokens_returned: int = Field(ge=0)
    budget_remaining_tokens: int = Field(ge=0)
    authorization: AuthorizationDecision
    denial_reason: str | None = None

    @model_validator(mode="after")
    def _denied_requires_reason(self) -> Self:
        if self.status == "denied" and not self.denial_reason:
            raise ValueError("denial_reason is required when status is 'denied'")
        return self


class OpenEvidenceRequest(McpModel):
    context_pack_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)


class OpenEvidenceResponse(McpModel):
    evidence_id: str = Field(min_length=1)
    level: Literal["L2", "L3"]
    # named to keep the security rule visible in the contract: expanded text is
    # retrieved content and must never change tool policy or instructions
    untrusted_content: str
    tokens_used: int = Field(ge=0)
    budget_remaining_tokens: int = Field(ge=0)
    source_uri: str | None = None
    authorization: AuthorizationDecision
    # advisory markers from the broker's deterministic injection scan; the
    # content above is verbatim — flagging never rewrites retrieved text
    injection_flagged: bool = False
    injection_signals: list[str] = Field(default_factory=list)
