"""pr_plan_v1 — output of the PR Planner Agent."""

from pydantic import Field

from agentic_mcp_server.agent_output_schemas.base import (
    AgentOutputComponent,
    AgentOutputModel,
)


class PlannedPr(AgentOutputComponent):
    title: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)


class PrPlanV1(AgentOutputModel):
    prs: list[PlannedPr] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)
