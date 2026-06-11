"""phased_pr_plan_v1 — final synthesized output of the Orchestrator."""

from pydantic import Field

from agentic_mcp_server.agent_output_schemas.base import (
    AgentOutputComponent,
    AgentOutputModel,
    EvidencedClaim,
)


class PlanPhase(AgentOutputComponent):
    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    changes: list[EvidencedClaim] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class PhasedPrPlanV1(AgentOutputModel):
    goal: str = Field(min_length=1)
    phases: list[PlanPhase] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)
