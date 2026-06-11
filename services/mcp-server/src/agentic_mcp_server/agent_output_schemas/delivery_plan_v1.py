"""delivery_plan_v1 — output of the Delivery Planner Agent."""

from pydantic import Field

from agentic_mcp_server.agent_output_schemas.base import (
    AgentOutputComponent,
    AgentOutputModel,
    EvidencedClaim,
)


class RolloutStep(AgentOutputComponent):
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class DeliveryPlanV1(AgentOutputModel):
    rollout_steps: list[RolloutStep] = Field(min_length=1)
    monitoring: list[EvidencedClaim] = Field(default_factory=list[EvidencedClaim])
    risks: list[EvidencedClaim] = Field(default_factory=list[EvidencedClaim])
    open_questions: list[str] = Field(default_factory=list)
