"""adr_draft_v1 — output of the ADR Writer Agent."""

from typing import Literal

from pydantic import Field

from agentic_mcp_server.agent_output_schemas.base import (
    AgentOutputComponent,
    AgentOutputModel,
    EvidencedClaim,
)


class RejectedAlternative(AgentOutputComponent):
    alternative: str = Field(min_length=1)
    why_rejected: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class AdrDraftV1(AgentOutputModel):
    title: str = Field(min_length=1)
    # a draft is always a proposal — accepting an ADR is a human act, never the agent's
    status: Literal["proposed"] = "proposed"
    context: list[EvidencedClaim] = Field(min_length=1)
    decision: str = Field(min_length=1)
    consequences: list[EvidencedClaim] = Field(min_length=1)
    alternatives_rejected: list[RejectedAlternative] = Field(
        default_factory=list[RejectedAlternative]
    )
    follow_ups: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
