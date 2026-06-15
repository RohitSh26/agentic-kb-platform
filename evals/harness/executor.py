"""Executes one eval case against the real Context Broker.

Drives broker functions in-process (the same seam mcp-server's integration
tests use) with a FakeSearchClient and a migrated TEST_DATABASE_URL registry.
Denied / needs_human_approval outcomes and their tool errors are contractual
broker behavior, not case failures; only ledger rows with status "error" fail
a case (docs/contracts/evals-report.md).
"""

import logging
import os
import uuid
from dataclasses import dataclass

from agentic_mcp_server.agent_output_schemas import (
    AgentOutputValidationError,
    ImplementationPlanV1,
    ImplementationStep,
    validate_evidence_references,
)
from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.budgets import AgentAllowance, BudgetPolicy
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.evidence import open_evidence
from agentic_mcp_server.context_broker.pack import create_pack
from agentic_mcp_server.context_broker.platform_trust import evaluate_platform_trust
from agentic_mcp_server.context_broker.request_more import request_more
from agentic_mcp_server.context_broker.verify import verify_answer
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.context import (
    CreatePackRequest,
    OpenEvidenceRequest,
    RequestMoreRequest,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimInput,
    VerifyAnswerRequest,
)
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness.cases import (
    UNKNOWN_EVIDENCE_PREFIX,
    EvalCase,
    OpenEvidenceStep,
    PlatformTrustStep,
    RequestMoreStep,
    VerifyAnswerStep,
)
from harness.fixtures import KB_VERSION, clean_registry, seed_case_fixtures
from harness.records import LedgerEvent, RunRecord

logger = logging.getLogger(__name__)

ORCHESTRATOR_SUBJECT = "orchestrator"

# A verification_required client used to exercise the platform-trust gate (F2).
# The signing key is set in-process by the platform_trust step (never literalised
# into committed fixtures) so a passing receipt can be signed + client-bound.
TRUST_CLIENT_ID = "eval-official-client"


# Manifest allowances from .claude/rules/token-budgets.md, keyed by the agent names
# case scripts use as broker subjects (each value is the top of its documented range;
# test_allowances.py pins that they stay within the rule):
#   impl-agent     -> implementation: 2 req / 3k-4k
#   test-agent     -> test:           1 req / 1.5k-2.5k
#   review-agent   -> code reviewer:  1 req / 1.5k-2.5k
#   delivery-agent -> delivery planner: 1 req / 1k-1.5k
#   pr-planner-agent -> PR planner:    1 req / 1k-1.5k
AGENT_ALLOWANCES: dict[str, AgentAllowance] = {
    "impl-agent": AgentAllowance(max_requests=2, max_tokens=4000),
    "test-agent": AgentAllowance(max_requests=1, max_tokens=2500),
    "review-agent": AgentAllowance(max_requests=1, max_tokens=2500),
    "delivery-agent": AgentAllowance(max_requests=1, max_tokens=1500),
    "pr-planner-agent": AgentAllowance(max_requests=1, max_tokens=1500),
}


def _requester(subject: str, teams: frozenset[str] = frozenset()) -> Requester:
    return Requester(subject=subject, teams=teams)


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
    # the orchestrator/requester carries the case's team memberships (team_acl_v1):
    # a team-less requester sees only org-public artifacts, which is how a
    # must_not_leak (F7) case proves a restricted artifact is filtered out.
    requester_teams = frozenset(case.requester_teams)

    corpus_parts: list[str] = []
    open_questions: list[str] = []
    # assertions a scripted trust step made (verify_answer / platform_trust /
    # must_not_leak); any failure flips the case to failed.
    step_failures: list[str] = []

    pack = await create_pack(
        deps,
        CreatePackRequest(
            run_id=case.id,
            task=case.task,
            approved_context_plan=case.approved_context_plan,
            retrieval_profile="default",
            budget_tokens=case.budget_tokens,
        ),
        _requester(ORCHESTRATOR_SUBJECT, requester_teams),
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
                _requester(step.agent, requester_teams),
            )
            corpus_parts += [f"{card.title} {card.summary}" for card in response.new_evidence_cards]
            logger.info(
                "eval.step case=%s tool=context.request_more status=%s", case.id, response.status
            )
        elif isinstance(step, OpenEvidenceStep):
            await _open_evidence_step(
                deps, case, pack.context_pack_id, step, key_to_id, requester_teams, corpus_parts
            )
        elif isinstance(step, VerifyAnswerStep):
            await _verify_answer_step(
                deps, case, step, key_to_id, requester_teams, session_factory, step_failures
            )
        else:
            await _platform_trust_step(
                deps, case, step, key_to_id, requester_teams, session_factory, step_failures
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

    # ACL negative (F7): a must_not_leak fixture must NEVER reach a returned card.
    for key in case.must_not_leak:
        if str(key_to_id[key]) in returned_ids:
            step_failures.append(f"acl_leak:{key}")
            logger.info("eval.case case=%s acl_leak key=%s", case.id, key)

    missing = _missing_items(case, key_to_id, returned_ids, " ".join(corpus_parts), open_questions)
    doc_recall_complete = not any(item.startswith("doc:") for item in missing)
    succeeded = (
        doc_recall_complete
        and not step_failures
        and all(event.status != "error" for event in events)
    )

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
    requester_teams: frozenset[str],
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
            _requester(step.agent, requester_teams),
        )
    except ToolError as error:
        # denial paths raise by contract; the ledger row carries the outcome
        logger.info("eval.step case=%s tool=context.open_evidence denied=%s", case.id, error)
        return
    corpus_parts.append(response.untrusted_content)


async def _seed_requester_ledger(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    subject: str,
    artifact_ids: list[uuid.UUID],
    kb_version: str,
) -> None:
    """Record that ``subject`` retrieved these ids (a ledger row), so verify_answer's
    L0_in_requester_ledger check can pass. Omitting an id here is how a case scripts
    the F1 negative: an answer citing evidence the requester never retrieved."""
    if not artifact_ids:
        return
    async with session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=run_id,
                agent_name=subject,
                tool_name="context.create_pack",
                status="approved",
                kb_version=kb_version,
                returned_artifact_ids=artifact_ids,
            ),
        )


async def _verify_answer_step(
    deps: BrokerDeps,
    case: EvalCase,
    step: VerifyAnswerStep,
    key_to_id: dict[str, uuid.UUID],
    requester_teams: frozenset[str],
    session_factory: async_sessionmaker[AsyncSession],
    step_failures: list[str],
) -> None:
    """Drive context.verify_answer and assert its overall verdict (F1).

    Seeds a ledger row for the `retrieved` keys first (so L0_in_requester_ledger can
    pass); a key cited but NOT listed in `retrieved` fails that L0 check — the F1
    failing-receipt negative. The verifier writes its own retrieval_event."""
    await _seed_requester_ledger(
        session_factory,
        run_id=case.id,
        subject=step.agent,
        artifact_ids=[key_to_id[key] for key in step.retrieved],
        kb_version=KB_VERSION,
    )
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id=step.answer_id,
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text=step.claim,
                    evidence_ids=[str(key_to_id[key]) for key in step.evidence],
                )
            ],
            verifier_levels=list(step.verifier_levels),
        ),
        _requester(step.agent, requester_teams),
    )
    logger.info(
        "eval.step case=%s tool=context.verify_answer overall=%s expected=%s",
        case.id,
        receipt.overall,
        step.expect_overall,
    )
    if receipt.overall != step.expect_overall:
        step_failures.append(
            f"verify_answer:{step.answer_id}:{receipt.overall}!={step.expect_overall}"
        )


async def _platform_trust_step(
    deps: BrokerDeps,
    case: EvalCase,
    step: PlatformTrustStep,
    key_to_id: dict[str, uuid.UUID],
    requester_teams: frozenset[str],
    session_factory: async_sessionmaker[AsyncSession],
    step_failures: list[str],
) -> None:
    """Drive the context.platform_trust gate and assert its status (F2).

    A verification_required client is `trusted` ONLY with a valid, client-matched,
    passing, SIGNED receipt; with no receipt it is `denied` (a structured refusal,
    never a silent pass). The signing key is set in-process here so the receipt can
    be signed and client-bound — never read from a committed secret."""
    client = ClientIdentity(
        client_id=TRUST_CLIENT_ID,
        verification_required=step.verification_required,
        registered=True,
    )
    receipt = None
    if step.present_receipt:
        # The signing key NAME is config; we set a non-secret test VALUE in-process
        # so verify_answer signs + client-binds the receipt and the gate can validate
        # it statelessly. Restored afterwards so the env leaks nothing across cases.
        signing_env = deps.settings.signing_key_env
        previous = os.environ.get(signing_env)
        os.environ[signing_env] = "eval-test-signing-key"
        try:
            await _seed_requester_ledger(
                session_factory,
                run_id=case.id,
                subject=step.agent,
                artifact_ids=[key_to_id[key] for key in step.retrieved],
                kb_version=KB_VERSION,
            )
            receipt = await verify_answer(
                deps,
                VerifyAnswerRequest(
                    answer_id=step.answer_id,
                    claims=[
                        ClaimInput(
                            claim_id="c1",
                            text=step.claim,
                            evidence_ids=[str(key_to_id[key]) for key in step.evidence],
                        )
                    ],
                ),
                _requester(step.agent, requester_teams),
                client,
            )
            decision = evaluate_platform_trust(client, receipt, signing_key_env=signing_env)
        finally:
            if previous is None:
                os.environ.pop(signing_env, None)
            else:
                os.environ[signing_env] = previous
    else:
        decision = evaluate_platform_trust(
            client, None, signing_key_env=deps.settings.signing_key_env
        )
    logger.info(
        "eval.step case=%s tool=context.platform_trust status=%s expected=%s",
        case.id,
        decision.status,
        step.expect_status,
    )
    if decision.status != step.expect_status:
        step_failures.append(f"platform_trust:{decision.status}!={step.expect_status}")


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
    # non-doc recall is presence in the broker-returned corpus (pack summary + card
    # title/summary + expansion text assembled in execute_case — never scripted claims);
    # doc recall above is the ID-grounded check. Substring presence is a deliberate V1
    # approximation (a returned-card anchor would need a per-item card reference).
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
