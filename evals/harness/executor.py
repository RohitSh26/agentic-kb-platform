"""Executes one eval case against the real Context Broker.

Drives broker functions in-process (the same seam mcp-server's integration
tests use) with a FakeSearchClient and a migrated TEST_DATABASE_URL registry.
Denied / needs_human_approval outcomes and their tool errors are contractual
broker behavior, not case failures; only ledger rows with status "error" fail
a case (docs/contracts/evals-report.md).
"""

import logging
import uuid
from dataclasses import dataclass

from agentic_mcp_server.agent_output_schemas import (
    AgentOutputValidationError,
    ImplementationPlanV1,
    ImplementationStep,
    validate_evidence_references,
)
from agentic_mcp_server.context_broker.budgets import AgentAllowance, BudgetPolicy
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.evidence import open_evidence
from agentic_mcp_server.context_broker.pack import create_pack
from agentic_mcp_server.context_broker.request_more import request_more
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    OpenEvidenceRequest,
    RequestMoreRequest,
)
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness.cases import (
    UNKNOWN_EVIDENCE_PREFIX,
    EvalCase,
    OpenEvidenceStep,
    RequestMoreStep,
)
from harness.fixtures import clean_registry, seed_case_fixtures
from harness.records import LedgerEvent, RunRecord

logger = logging.getLogger(__name__)

ORCHESTRATOR_SUBJECT = "orchestrator"

# manifest allowances from .claude/rules/token-budgets.md, keyed by the agent
# names case scripts use as broker subjects
AGENT_ALLOWANCES: dict[str, AgentAllowance] = {
    "impl-agent": AgentAllowance(max_requests=2, max_tokens=4000),
    "test-agent": AgentAllowance(max_requests=1, max_tokens=2500),
    "review-agent": AgentAllowance(max_requests=1, max_tokens=2500),
    "delivery-agent": AgentAllowance(max_requests=1, max_tokens=1500),
    "pr-planner-agent": AgentAllowance(max_requests=1, max_tokens=1500),
}


@dataclass(frozen=True)
class CaseResult:
    record: RunRecord
    tokens_charged: int


async def execute_case(
    case: EvalCase, session_factory: async_sessionmaker[AsyncSession]
) -> CaseResult:
    search = FakeSearchClient()
    async with session_factory() as session:
        await clean_registry(session)
        key_to_id = await seed_case_fixtures(session, case.fixtures, search)
    deps = BrokerDeps(
        session_factory=session_factory,
        search_client=search,
        budget_policy=BudgetPolicy(allowances=AGENT_ALLOWANCES),
    )

    corpus_parts: list[str] = []
    open_questions: list[str] = []

    pack = await create_pack(
        deps,
        CreatePackRequest(
            run_id=case.id,
            task=case.task,
            approved_context_plan=case.approved_context_plan,
            retrieval_profile="default",
            budget_tokens=case.budget_tokens,
        ),
        ORCHESTRATOR_SUBJECT,
    )
    corpus_parts.append(pack.summary)
    corpus_parts += [f"{card.title} {card.summary}" for card in pack.evidence_cards]
    open_questions += pack.open_questions

    for step in case.script:
        if isinstance(step, RequestMoreStep):
            response = await request_more(
                deps,
                RequestMoreRequest(
                    context_pack_id=pack.context_pack_id,
                    agent_name=step.agent,
                    question=step.question,
                    why_needed=step.why_needed,
                    decision_needed=step.decision_needed,
                    already_checked_evidence_ids=[
                        str(key_to_id[key]) for key in step.already_checked
                    ],
                    max_tokens=step.max_tokens,
                ),
                step.agent,
            )
            corpus_parts += [f"{card.title} {card.summary}" for card in response.new_evidence_cards]
            logger.info(
                "eval.step case=%s tool=context.request_more status=%s", case.id, response.status
            )
        else:
            await _open_evidence_step(
                deps, case, pack.context_pack_id, step, key_to_id, corpus_parts
            )

    if case.agent_output is not None:
        open_questions += case.agent_output.open_questions

    events = await _fetch_events(session_factory, case.id)
    returned_ids = {
        evidence_id
        for event in events
        if event.status != "error"
        for evidence_id in event.evidence_ids
    }

    missing = _missing_items(case, key_to_id, returned_ids, " ".join(corpus_parts), open_questions)
    doc_recall_complete = not any(item.startswith("doc:") for item in missing)
    succeeded = doc_recall_complete and all(event.status != "error" for event in events)

    total_claims, unsupported = _validate_claims(case, key_to_id, returned_ids)

    record = RunRecord(
        case_id=case.id,
        task_type=case.task_type,
        succeeded=succeeded,
        expected_items=_expected_count(case),
        missing_items=tuple(missing),
        total_claims=total_claims,
        unsupported_claims=unsupported,
        events=tuple(events),
    )
    logger.info(
        "eval.case case=%s succeeded=%s missing=%d tokens=%d",
        case.id,
        succeeded,
        len(missing),
        record.tokens_charged,
    )
    return CaseResult(record=record, tokens_charged=record.tokens_charged)


async def _open_evidence_step(
    deps: BrokerDeps,
    case: EvalCase,
    context_pack_id: str,
    step: OpenEvidenceStep,
    key_to_id: dict[str, uuid.UUID],
    corpus_parts: list[str],
) -> None:
    try:
        response = await open_evidence(
            deps,
            OpenEvidenceRequest(
                context_pack_id=context_pack_id,
                evidence_id=str(key_to_id[step.evidence]),
                max_tokens=step.max_tokens,
            ),
            step.agent,
        )
    except ToolError as error:
        # denial paths raise by contract; the ledger row carries the outcome
        logger.info("eval.step case=%s tool=context.open_evidence denied=%s", case.id, error)
        return
    corpus_parts.append(response.untrusted_content)


async def _fetch_events(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> list[LedgerEvent]:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT tool_name, status, agent_name, cache_hit, semantic_reuse,"
                " tokens_returned, reused_evidence_ids, new_evidence_ids"
                " FROM retrieval_event WHERE run_id = :run_id"
                " ORDER BY created_at, retrieval_id"
            ),
            {"run_id": run_id},
        )
        rows = result.mappings().all()
    return [
        LedgerEvent(
            tool_name=row["tool_name"],
            status=row["status"],
            agent_name=row["agent_name"],
            cache_hit=row["cache_hit"],
            semantic_reuse=row["semantic_reuse"],
            tokens_returned=row["tokens_returned"],
            reused_evidence_ids=tuple(str(e) for e in row["reused_evidence_ids"] or ()),
            new_evidence_ids=tuple(str(e) for e in row["new_evidence_ids"] or ()),
        )
        for row in rows
    ]


def _expected_count(case: EvalCase) -> int:
    expected = case.expected
    return (
        len(expected.docs)
        + len(expected.files)
        + len(expected.symbols)
        + len(expected.tests)
        + len(expected.open_questions)
    )


def _missing_items(
    case: EvalCase,
    key_to_id: dict[str, uuid.UUID],
    returned_ids: set[str],
    corpus: str,
    open_questions: list[str],
) -> list[str]:
    missing: list[str] = []
    for key in case.expected.docs:
        if str(key_to_id[key]) not in returned_ids:
            missing.append(f"doc:{key}")
    folded_corpus = corpus.casefold()
    for category, items in (
        ("file", case.expected.files),
        ("symbol", case.expected.symbols),
        ("test", case.expected.tests),
    ):
        missing += [f"{category}:{item}" for item in items if item.casefold() not in folded_corpus]
    folded_questions = [question.casefold() for question in open_questions]
    missing += [
        f"question:{expected}"
        for expected in case.expected.open_questions
        if not any(expected.casefold() in question for question in folded_questions)
    ]
    return missing


def _validate_claims(
    case: EvalCase, key_to_id: dict[str, uuid.UUID], returned_ids: set[str]
) -> tuple[int, int]:
    """Validate scripted claims through the PR-11 seam against broker-returned IDs."""
    if case.agent_output is None:
        return 0, 0
    unsupported = 0
    for claim in case.agent_output.claims:
        evidence_ids = [
            handle if handle.startswith(UNKNOWN_EVIDENCE_PREFIX) else str(key_to_id[handle])
            for handle in claim.evidence
        ]
        output = ImplementationPlanV1(
            task=case.task,
            steps=[ImplementationStep(description=claim.claim, evidence_ids=evidence_ids)],
        )
        try:
            validate_evidence_references(output, known_evidence_ids=returned_ids)
        except AgentOutputValidationError:
            unsupported += 1
    return len(case.agent_output.claims), unsupported
