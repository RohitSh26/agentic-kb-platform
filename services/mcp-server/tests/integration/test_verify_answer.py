"""context.verify_answer: deterministic L0 provenance verifier (ADR-0011).

The verifier is the trust boundary (docs/contracts/verification-receipt.md):
an answer is platform-trusted iff it carries a valid receipt. These tests cover
each of the six L0 checks, the passed/failed/partial roll-up, the answer_hash
stability guarantee, schema rejection, and the mandatory ledger row.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    fetch_ledger_rows,
    insert_artifact,
    insert_build_run,
    insert_edge,
    make_broker_deps,
    require_registry_schema,
)
from fastmcp.exceptions import ToolError
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.verify import NO_RUN_SENTINEL, verify_answer
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimInput,
    VerifyAnswerRequest,
)

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())
OTHER_VERSION = "kb-old"


@pytest.fixture()
def factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await insert_build_run(session, KB_VERSION, "active")
    yield


async def _seed_supported_artifact(
    session: AsyncSession,
    *,
    kb_version: str = KB_VERSION,
    acl_teams: list[str] | None = None,
    source_is_deleted: bool = False,
    with_extracted_edge: bool = True,
    inferred_only: bool = False,
) -> uuid.UUID:
    """An artifact plus, by default, an incident EXTRACTED edge (claim support).

    ``inferred_only`` instead gives it only an INFERRED_HIGH edge, so it exists
    but is supported solely by a routing hint (L0_supporting_trust_ok = false).
    """
    artifact = await insert_artifact(
        session,
        kb_version=kb_version,
        title="evidence",
        body_text="x",
        acl_teams=acl_teams,
        source_is_deleted=source_is_deleted,
    )
    neighbor = await insert_artifact(
        session, kb_version=kb_version, title="neighbor", body_text="y"
    )
    if inferred_only:
        await insert_edge(
            session,
            from_artifact_id=artifact,
            to_artifact_id=neighbor,
            edge_type="documents",
            kb_version=kb_version,
            trust_class="INFERRED_HIGH",
        )
    elif with_extracted_edge:
        await insert_edge(
            session,
            from_artifact_id=artifact,
            to_artifact_id=neighbor,
            edge_type="calls",
            kb_version=kb_version,
            trust_class="EXTRACTED",
        )
    return artifact


async def _record_retrieval(
    factory: async_sessionmaker[AsyncSession],
    artifact_ids: list[uuid.UUID],
    *,
    subject: str = SUBJECT,
) -> None:
    """Simulate that these ids were returned to ``subject`` via the ledger."""
    async with factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id="run-seed",
                agent_name=subject,
                tool_name="context.create_pack",
                status="approved",
                kb_version=KB_VERSION,
                returned_artifact_ids=artifact_ids,
            ),
        )


def _claim(claim_id: str, evidence_ids: list[uuid.UUID]) -> ClaimInput:
    return ClaimInput(
        claim_id=claim_id, text="some claim", evidence_ids=[str(e) for e in evidence_ids]
    )


# ---------------------------------------------------------------------------
# Happy path + roll-up
# ---------------------------------------------------------------------------


async def test_valid_retrieved_extracted_evidence_passes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _seed_supported_artifact(session)
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="ans-1", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    assert receipt.receipt_schema_version == 1
    assert receipt.graph_version == KB_VERSION
    assert receipt.verifier_levels_run == ["L0"]
    assert receipt.client_id is None
    assert receipt.signature is None
    assert receipt.overall == "passed"
    (result,) = receipt.claim_results
    assert result.result == "passed"
    assert result.failed_reasons == []
    checks = result.checks
    assert checks.L0_exists
    assert checks.L0_in_active_version
    assert checks.L0_acl_visible
    assert checks.L0_in_requester_ledger
    assert checks.L0_not_stale
    assert checks.L0_supporting_trust_ok


async def test_standalone_evidence_with_no_edges_supports_a_claim(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Edgeless source-backed evidence (e.g. a wikify summary) is NOT inferred, so it
    # must support a claim — only evidence reached solely via inferred edges fails.
    async with factory() as session:
        evidence = await _seed_supported_artifact(session, with_extracted_edge=False)
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="ans-no-edge", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    assert receipt.overall == "passed"
    assert receipt.claim_results[0].checks.L0_supporting_trust_ok


async def test_mixed_claims_yield_partial(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        good = await _seed_supported_artifact(session)
        bad = await _seed_supported_artifact(session)  # never retrieved
    await _record_retrieval(factory, [good])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="ans-mixed",
            claims=[_claim("good", [good]), _claim("bad", [bad])],
        ),
        REQUESTER,
    )

    assert receipt.overall == "partial"
    by_claim = {r.claim_id: r for r in receipt.claim_results}
    assert by_claim["good"].result == "passed"
    assert by_claim["bad"].result == "failed"


# ---------------------------------------------------------------------------
# Each L0 failure mode flips to failed with the right reason
# ---------------------------------------------------------------------------


async def test_evidence_not_retrieved_by_requester_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _seed_supported_artifact(session)
    # Retrieved by a DIFFERENT subject only — not by this requester.
    await _record_retrieval(factory, [evidence], subject="other-agent")
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    (result,) = receipt.claim_results
    assert result.result == "failed"
    assert receipt.overall == "failed"
    assert result.checks.L0_in_requester_ledger is False
    assert "evidence_not_retrieved_by_requester" in result.failed_reasons


async def test_evidence_from_another_version_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _seed_supported_artifact(session, kb_version=OTHER_VERSION)
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    (result,) = receipt.claim_results
    assert result.result == "failed"
    # It exists (somewhere) but not in the active version.
    assert result.checks.L0_exists is True
    assert result.checks.L0_in_active_version is False
    assert "evidence_from_another_version" in result.failed_reasons


async def test_nonexistent_evidence_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ghost = uuid.uuid4()
    await _record_retrieval(factory, [ghost])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [ghost])]),
        REQUESTER,
    )

    (result,) = receipt.claim_results
    assert result.result == "failed"
    assert result.checks.L0_exists is False
    assert "evidence_not_found" in result.failed_reasons


async def test_acl_invisible_evidence_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _seed_supported_artifact(session, acl_teams=["secret-team"])
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    # Requester carries no teams ⇒ cannot see a team-restricted artifact.
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    (result,) = receipt.claim_results
    assert result.result == "failed"
    assert result.checks.L0_acl_visible is False
    assert "evidence_acl_invisible" in result.failed_reasons


async def test_stale_evidence_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _seed_supported_artifact(session, source_is_deleted=True)
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    (result,) = receipt.claim_results
    assert result.result == "failed"
    assert result.checks.L0_not_stale is False
    assert "evidence_stale" in result.failed_reasons


async def test_inferred_only_support_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _seed_supported_artifact(session, inferred_only=True)
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    (result,) = receipt.claim_results
    assert result.result == "failed"
    # Exists/in-version/visible/retrieved, but supported only by an INFERRED hint.
    assert result.checks.L0_in_active_version is True
    assert result.checks.L0_supporting_trust_ok is False
    assert "evidence_supported_only_by_inferred_edge" in result.failed_reasons


async def test_claim_fails_if_any_cited_evidence_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A claim passes iff ALL cited evidence passes ALL checks."""
    async with factory() as session:
        good = await _seed_supported_artifact(session)
        stale = await _seed_supported_artifact(session, source_is_deleted=True)
    await _record_retrieval(factory, [good, stale])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [good, stale])]),
        REQUESTER,
    )

    (result,) = receipt.claim_results
    assert result.result == "failed"
    assert result.checks.L0_not_stale is False
    # The good evidence still satisfies the other checks at the claim level.
    assert result.checks.L0_supporting_trust_ok is True


# ---------------------------------------------------------------------------
# Schema rejection, answer_hash, ledger
# ---------------------------------------------------------------------------


def test_request_with_no_claims_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VerifyAnswerRequest(answer_id="a", claims=[])


def test_claim_with_empty_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VerifyAnswerRequest(
            answer_id="a", claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[])]
        )


async def test_answer_hash_is_stable_for_the_same_normalized_claims(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        e1 = await _seed_supported_artifact(session)
        e2 = await _seed_supported_artifact(session)
    await _record_retrieval(factory, [e1, e2])
    deps = make_broker_deps(factory, FakeSearchClient())

    base = VerifyAnswerRequest(
        answer_id="ans-A",
        claims=[
            ClaimInput(
                claim_id="c1", text="the api validates the payload", evidence_ids=[str(e1), str(e2)]
            ),
            ClaimInput(claim_id="c2", text="and logs", evidence_ids=[str(e1)]),
        ],
    )
    # Different answer_id, claim ordering, evidence-id ordering, and whitespace,
    # but the same normalized claims ⇒ the same hash.
    permuted = VerifyAnswerRequest(
        answer_id="ans-B",
        claims=[
            ClaimInput(claim_id="c2", text="and   logs", evidence_ids=[str(e1)]),
            ClaimInput(
                claim_id="c1",
                text=" the api validates the payload ",
                evidence_ids=[str(e2), str(e1), str(e1)],
            ),
        ],
    )

    first = await verify_answer(deps, base, REQUESTER)
    second = await verify_answer(deps, permuted, REQUESTER)
    assert first.answer_hash == second.answer_hash

    # A genuinely different claim text changes the hash.
    different = VerifyAnswerRequest(
        answer_id="ans-A",
        claims=[
            ClaimInput(
                claim_id="c1", text="the api rejects the payload", evidence_ids=[str(e1), str(e2)]
            ),
            ClaimInput(claim_id="c2", text="and logs", evidence_ids=[str(e1)]),
        ],
    )
    third = await verify_answer(deps, different, REQUESTER)
    assert third.answer_hash != first.answer_hash


async def test_every_call_writes_a_ledger_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _seed_supported_artifact(session)
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="ans-ledger", claims=[_claim("c1", [evidence])]),
        REQUESTER,
    )

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    verify_rows = [r for r in rows if r.tool_name == "context.verify_answer"]
    assert [(r.tool_name, r.status, r.agent_name) for r in verify_rows] == [
        ("context.verify_answer", "approved", SUBJECT)
    ]


async def test_no_active_kb_version_errors_and_audits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await session.execute(text("DELETE FROM kb_build_run"))
        await session.commit()
    deps = make_broker_deps(factory, FakeSearchClient())

    with pytest.raises(ToolError, match="no active kb_version"):
        await verify_answer(
            deps,
            VerifyAnswerRequest(answer_id="a", claims=[_claim("c1", [uuid.uuid4()])]),
            REQUESTER,
        )

    async with factory() as session:
        rows = await fetch_ledger_rows(session, NO_RUN_SENTINEL)
    assert any(r.tool_name == "context.verify_answer" and r.status == "error" for r in rows)
