"""Agent output schemas: the structured outputs the MCP runtime accepts.

Registry keys are the output_schema names declared in agents/*.md manifests —
contract of record: docs/contracts/agent-output-contracts.md.
"""

from agentic_mcp_server.agent_output_schemas.adr_draft_v1 import (
    AdrDraftV1,
    RejectedAlternative,
)
from agentic_mcp_server.agent_output_schemas.base import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    AgentOutputComponent,
    AgentOutputModel,
    AgentOutputValidationError,
    EvidencedClaim,
    referenced_evidence_ids,
    validate_evidence_references,
)
from agentic_mcp_server.agent_output_schemas.delivery_plan_v1 import DeliveryPlanV1, RolloutStep
from agentic_mcp_server.agent_output_schemas.implementation_plan_v1 import (
    ImplementationPlanV1,
    ImplementationStep,
)
from agentic_mcp_server.agent_output_schemas.phased_pr_plan_v1 import PhasedPrPlanV1, PlanPhase
from agentic_mcp_server.agent_output_schemas.pr_plan_v1 import PlannedPr, PrPlanV1
from agentic_mcp_server.agent_output_schemas.review_findings_v1 import (
    ReviewFinding,
    ReviewFindingsV1,
)
from agentic_mcp_server.agent_output_schemas.test_plan_v1 import PlannedTest, TestPlanV1

AGENT_OUTPUT_SCHEMAS: dict[str, type[AgentOutputModel]] = {
    "phased_pr_plan_v1": PhasedPrPlanV1,
    "implementation_plan_v1": ImplementationPlanV1,
    "test_plan_v1": TestPlanV1,
    "review_findings_v1": ReviewFindingsV1,
    "delivery_plan_v1": DeliveryPlanV1,
    "pr_plan_v1": PrPlanV1,
    "adr_draft_v1": AdrDraftV1,
}

__all__ = [
    "AGENT_OUTPUT_SCHEMAS",
    "AGENT_OUTPUT_SCHEMA_VERSION",
    "AdrDraftV1",
    "AgentOutputComponent",
    "AgentOutputModel",
    "AgentOutputValidationError",
    "DeliveryPlanV1",
    "EvidencedClaim",
    "ImplementationPlanV1",
    "ImplementationStep",
    "PhasedPrPlanV1",
    "PlanPhase",
    "PlannedPr",
    "PlannedTest",
    "PrPlanV1",
    "RejectedAlternative",
    "ReviewFinding",
    "ReviewFindingsV1",
    "RolloutStep",
    "TestPlanV1",
    "referenced_evidence_ids",
    "validate_evidence_references",
]
