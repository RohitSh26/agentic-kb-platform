"""implementation_plan_v1 — output of the Implementation Agent."""

from pydantic import Field

from agentic_mcp_server.agent_output_schemas.base import (
    AgentOutputComponent,
    AgentOutputModel,
    EvidencedClaim,
)


class ImplementationStep(AgentOutputComponent):
    description: str = Field(min_length=1)
    target_artifacts: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)


class ImplementationPlanV1(AgentOutputModel):
    task: str = Field(min_length=1)
    steps: list[ImplementationStep] = Field(min_length=1)
    risks: list[EvidencedClaim] = Field(default_factory=list[EvidencedClaim])
    open_questions: list[str] = Field(default_factory=list)
