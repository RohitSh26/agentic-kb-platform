"""review_findings_v1 — output of the Code Reviewer Agent."""

from typing import Literal

from pydantic import Field

from agentic_mcp_server.agent_output_schemas.base import (
    AgentOutputComponent,
    AgentOutputModel,
)

Severity = Literal["blocker", "major", "minor", "note"]


class ReviewFinding(AgentOutputComponent):
    severity: Severity
    finding: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ReviewFindingsV1(AgentOutputModel):
    verdict: Literal["approve", "request_changes"]
    findings: list[ReviewFinding] = Field(default_factory=list[ReviewFinding])
    open_questions: list[str] = Field(default_factory=list)
