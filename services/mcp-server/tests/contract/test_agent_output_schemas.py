"""Agent output schemas: every claim cites evidence, unknown handles fail.

Executable spec for docs/contracts/agent-output-contracts.md — claims are
unconstructible without evidence (missing evidence becomes an open question,
never an invention), and outputs citing handles the pack never returned are
rejected.
"""

import pytest
from pydantic import ValidationError

from agentic_mcp_server.agent_output_schemas import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMAS,
    AgentOutputModel,
    AgentOutputValidationError,
    EvidencedClaim,
    ImplementationPlanV1,
    ImplementationStep,
    PhasedPrPlanV1,
    PlannedPr,
    PlannedTest,
    PlanPhase,
    PrPlanV1,
    ReviewFinding,
    ReviewFindingsV1,
    RolloutStep,
    referenced_evidence_ids,
    validate_evidence_references,
)

EVIDENCE_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


def _plan(evidence_id: str = EVIDENCE_ID) -> ImplementationPlanV1:
    return ImplementationPlanV1(
        task="add refund validation",
        steps=[
            ImplementationStep(
                description="extend the validator",
                target_artifacts=["checkout/validators.py"],
                evidence_ids=[evidence_id],
            )
        ],
        open_questions=["is the 24h refund window enforced anywhere else?"],
    )


def test_registry_covers_all_six_schemas_and_extends_the_base() -> None:
    assert set(AGENT_OUTPUT_SCHEMAS) == {
        "phased_pr_plan_v1",
        "implementation_plan_v1",
        "test_plan_v1",
        "review_findings_v1",
        "delivery_plan_v1",
        "pr_plan_v1",
    }
    for model in AGENT_OUTPUT_SCHEMAS.values():
        assert issubclass(model, AgentOutputModel)
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"


def test_outputs_carry_the_schema_version() -> None:
    assert _plan().schema_version == AGENT_OUTPUT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "build",
    [
        lambda: EvidencedClaim(claim="x", evidence_ids=[]),
        lambda: ImplementationStep(description="x", evidence_ids=[]),
        lambda: PlannedTest(name="x", expectation="y", evidence_ids=[]),
        lambda: ReviewFinding(severity="major", finding="x", evidence_ids=[]),
        lambda: RolloutStep(description="x", evidence_ids=[]),
        lambda: PlannedPr(title="x", scope="y", evidence_ids=[]),
        lambda: PlanPhase(
            name="x",
            goal="y",
            changes=[EvidencedClaim(claim="z", evidence_ids=[])],
        ),
    ],
)
def test_a_claim_without_evidence_cannot_be_constructed(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


def test_missing_evidence_is_expressed_as_an_open_question_not_a_claim() -> None:
    # an agent with nothing provable still produces valid output: zero claims,
    # open questions instead of invented facts
    output = ReviewFindingsV1(
        verdict="request_changes",
        findings=[],
        open_questions=["no evidence found for the webhook retry behavior"],
    )
    assert referenced_evidence_ids(output) == set()
    validate_evidence_references(output, known_evidence_ids=set())


def test_unknown_evidence_id_fails_validation() -> None:
    plan = _plan(evidence_id=OTHER_ID)
    with pytest.raises(AgentOutputValidationError, match=OTHER_ID):
        validate_evidence_references(plan, known_evidence_ids={EVIDENCE_ID})


def test_known_evidence_ids_pass_validation() -> None:
    validate_evidence_references(_plan(), known_evidence_ids={EVIDENCE_ID})


def test_referenced_evidence_ids_walks_nested_structures() -> None:
    plan = PhasedPrPlanV1(
        goal="ship refund validation",
        phases=[
            PlanPhase(
                name="phase 1",
                goal="validator",
                changes=[EvidencedClaim(claim="extend validator", evidence_ids=[EVIDENCE_ID])],
            ),
            PlanPhase(
                name="phase 2",
                goal="tests",
                changes=[EvidencedClaim(claim="add regression tests", evidence_ids=[OTHER_ID])],
            ),
        ],
    )
    assert referenced_evidence_ids(plan) == {EVIDENCE_ID, OTHER_ID}


def test_unexpected_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PrPlanV1(
            prs=[PlannedPr(title="x", scope="y", evidence_ids=[EVIDENCE_ID])],
            confidence="high",  # type: ignore[call-arg]
        )


def test_outputs_are_immutable() -> None:
    plan = _plan()
    with pytest.raises(ValidationError):
        plan.task = "rewritten"  # type: ignore[misc]
