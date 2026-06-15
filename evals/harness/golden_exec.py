"""Execute golden-query cases through the real Context Broker (publish gate).

The golden set is the anti-underlinking publish gate (docs/contracts/golden-query-
evals.md, publish-gates.md): each case carries the EXPECTED evidence the broker
must surface and, optionally, ids that must NEVER leak. ``run.py`` previously only
*loaded* the golden set, so the evidence-recall floor was inert. This module wires
the missing execution: it seeds one artifact per expected / must-not-leak evidence
id, drives ``create_pack`` over the case query, maps the returned cards back to the
symbolic ids, and produces a :class:`GoldenResult` for ``harness.golden.aggregate``.

It is DB-aware (it seeds + reads the migrated TEST_DATABASE_URL registry) but
defers ALL scoring to ``harness.golden`` — the pinned metric functions are never
re-implemented here. A must-not-leak id is seeded as a team-restricted artifact, so
a requester lacking that team is filtered out by the broker's own ACL — the leak,
if any, is the broker's, not a harness artefact.
"""

import logging
import uuid

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.budgets import BudgetPolicy
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.pack import create_pack
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient, SearchHit
from agentic_mcp_server.mcp.tool_schemas.context import CreatePackRequest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness.fixtures import KB_VERSION, clean_registry
from harness.golden import GoldenCase, GoldenResult

logger = logging.getLogger(__name__)

ORCHESTRATOR_SUBJECT = "orchestrator"
# A team no golden requester carries: a must-not-leak artifact restricted to it is
# org-private, so a requester without it is ACL-filtered by the broker.
_RESTRICTED_TEAM = "eval-restricted-team"


async def _insert_golden_artifact(
    session: AsyncSession, *, evidence_id: str, acl_teams: list[str]
) -> uuid.UUID:
    """Seed one artifact whose title/body name the symbolic evidence id, returning
    its uuid. ``acl_teams`` empty ⇒ org-public; non-empty ⇒ team-restricted."""
    source_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO source_item (source_id, source_type, source_uri, source_version,"
            " content_hash) VALUES (CAST(:source_id AS uuid), 'github_code', :source_uri,"
            " 'rev-1', :content_hash)"
        ),
        {
            "source_id": str(source_id),
            "source_uri": f"https://example.test/golden/{evidence_id}",
            "content_hash": f"hash-{artifact_id}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO knowledge_artifact (artifact_id, artifact_type, source_id, title,"
            " body_text, kb_version, knowledge_kind, authority_score, acl_teams) VALUES"
            " (CAST(:artifact_id AS uuid), 'code_symbol', CAST(:source_id AS uuid), :title,"
            " :body_text, :kb_version, 'source_backed', 0.9, CAST(:acl_teams AS text[]))"
        ),
        {
            "artifact_id": str(artifact_id),
            "source_id": str(source_id),
            "title": evidence_id,
            "body_text": f"Golden evidence for {evidence_id}.",
            "kb_version": KB_VERSION,
            "acl_teams": acl_teams,
        },
    )
    return artifact_id


async def execute_golden_case(
    case: GoldenCase, session_factory: async_sessionmaker[AsyncSession]
) -> GoldenResult:
    """Seed the case's expected + must-not-leak evidence, run create_pack over the
    query, and return the GoldenResult the publish-gate metrics score."""
    search = FakeSearchClient()
    id_to_symbol: dict[uuid.UUID, str] = {}
    seed_keys: list[str] = []
    async with session_factory() as session:
        await clean_registry(session)
        await session.execute(
            text(
                "INSERT INTO kb_build_run (kb_version, build_seq, status)"
                " VALUES (:kb_version, 1, 'active')"
            ),
            {"kb_version": KB_VERSION},
        )
        for evidence_id in case.expected_evidence_ids:
            artifact_id = await _insert_golden_artifact(
                session, evidence_id=evidence_id, acl_teams=[]
            )
            id_to_symbol[artifact_id] = evidence_id
            seed_keys.append(evidence_id)
        for evidence_id in case.must_not_leak_ids:
            # team-restricted ⇒ a requester without _RESTRICTED_TEAM is ACL-filtered.
            artifact_id = await _insert_golden_artifact(
                session, evidence_id=evidence_id, acl_teams=[_RESTRICTED_TEAM]
            )
            id_to_symbol[artifact_id] = evidence_id
            seed_keys.append(evidence_id)
        await session.commit()

    # The whole-token FakeSearchClient matches the case query; seed every artifact as
    # a hit (expected first) so the broker, not the seed, decides what survives ACL +
    # the card cap. The broker hydrates from Postgres and filters before returning.
    hits = [
        SearchHit(artifact_id=artifact_id, score=float(len(seed_keys) - position))
        for position, artifact_id in enumerate(id_to_symbol)
    ]
    for token in case.query.split():
        search.seed(token, hits)

    deps = BrokerDeps(
        session_factory=session_factory,
        search_client=search,
        budget_policy=BudgetPolicy(),
    )
    pack = await create_pack(
        deps,
        CreatePackRequest(
            run_id=f"golden-{case.case_id.replace('/', '-')}",
            task=case.query,
            approved_context_plan=case.query,
            retrieval_profile="default",
            budget_tokens=8000,
        ),
        Requester(subject=ORCHESTRATOR_SUBJECT, teams=frozenset(case.requester_teams)),
    )
    returned = frozenset(
        id_to_symbol[card.artifact_id]
        for card in pack.evidence_cards
        if card.artifact_id in id_to_symbol
    )
    logger.info(
        "eval.golden case=%s returned=%d expected=%d leak_seeded=%d",
        case.case_id,
        len(returned),
        len(case.expected_evidence_ids),
        len(case.must_not_leak_ids),
    )
    return GoldenResult(case=case, returned_evidence_ids=returned)
