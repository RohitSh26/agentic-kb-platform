"""test_plan_v1 — output of the Test Layer Agent."""

from pydantic import Field

from agentic_mcp_server.agent_output_schemas.base import (
    AgentOutputComponent,
    AgentOutputModel,
    EvidencedClaim,
)


class PlannedTest(AgentOutputComponent):
    name: str = Field(min_length=1)
    expectation: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class TestPlanV1(AgentOutputModel):
    scope: str = Field(min_length=1)
    test_cases: list[PlannedTest] = Field(min_length=1)
    regression_risks: list[EvidencedClaim] = Field(default_factory=list[EvidencedClaim])
    open_questions: list[str] = Field(default_factory=list)
