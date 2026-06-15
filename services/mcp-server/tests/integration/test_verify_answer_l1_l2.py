"""context.verify_answer L1 (coverage + span cap) and L2 (typed fact), PR-30.

These cover the phase-4 additive levels on top of the L0 verifier
(verification-receipt.md): the claim/evidence ledger adjudicates typed facts
deterministically (no LLM). The headline case is a claim whose cited evidence is
real and retrieved but whose ASSERTION misreads it — L0 passes, L2 fails. We also
prove backward compatibility: an L0-only request runs exactly L0 and the receipt
shape is unchanged.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    insert_artifact,
    insert_build_run,
    insert_edge,
    make_broker_deps,
    require_registry_schema,
)
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerSettings
from agentic_mcp_server.context_broker.verify import verify_answer
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimInput,
    EdgeBetweenAssertion,
    FileImportsModuleAssertion,
    SymbolInFileAssertion,
    VerifyAnswerRequest,
)

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())


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


async def _record_retrieval(
    factory: async_sessionmaker[AsyncSession],
    artifact_ids: list[uuid.UUID],
    *,
    subject: str = SUBJECT,
) -> None:
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


async def _symbol_artifact(
    session: AsyncSession,
    *,
    name: str,
    path: str,
    acl_teams: list[str] | None = None,
) -> uuid.UUID:
    """A code_symbol artifact named ``name`` whose source path is ``path``.

    It is edgeless source-backed evidence, so it satisfies L0 supporting trust
    on its own — letting tests isolate the L2 typed-fact verdict.
    """
    return await insert_artifact(
        session,
        title=name,
        body_text="def x(): ...",
        artifact_type="code_symbol",
        knowledge_kind="source_backed",
        path=path,
        span_start=10,
        span_end=20,
        acl_teams=acl_teams,
    )


# ---------------------------------------------------------------------------
# Backward compatibility: L0-only request unchanged
# ---------------------------------------------------------------------------


async def test_l0_only_request_runs_only_l0_and_is_backward_compatible(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    # Default verifier_levels = ["L0"]; carries a quote + assertion that L1/L2
    # would react to, but neither runs because they were not requested.
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login is in auth.py",
                    evidence_ids=[str(evidence)],
                    quote="x" * 10_000,  # would blow the L1 cap if L1 ran
                    assertion=SymbolInFileAssertion(
                        kind="symbol_in_file", symbol="login", file="WRONG.py"
                    ),  # would fail L2 if L2 ran
                )
            ],
        ),
        REQUESTER,
    )

    assert receipt.verifier_levels_run == ["L0"]
    assert receipt.overall == "passed"
    checks = receipt.claim_results[0].checks
    # L0 keys present and pass; L1/L2 keys absent (None) — phase-1 shape.
    assert checks.L0_exists
    assert checks.L1_coverage is None
    assert checks.L2_typed_fact is None


# ---------------------------------------------------------------------------
# L1: citation coverage + span caps
# ---------------------------------------------------------------------------


async def test_l1_uncited_claim_fails_coverage(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Evidence exists/retrieved by ANOTHER subject only ⇒ not resolvable for this
    # requester ⇒ the claim cites no checkable unit ⇒ L1 coverage fails.
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence], subject="other-agent")
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    assert receipt.verifier_levels_run == ["L0", "L1"]
    result = receipt.claim_results[0]
    assert result.result == "failed"
    assert result.checks.L1_coverage is False
    assert "claim_uncited" in result.failed_reasons


async def test_l1_quote_over_cap_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    # Tight cap so a modest quote exceeds it.
    deps = make_broker_deps(factory, FakeSearchClient(), settings=BrokerSettings(max_quote_chars=8))

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="t",
                    evidence_ids=[str(evidence)],
                    quote="this quote is far too long",
                )
            ],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "failed"
    assert result.checks.L1_coverage is False
    assert "quote_over_cap" in result.failed_reasons


async def test_l1_within_cap_and_cited_passes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="t",
                    evidence_ids=[str(evidence)],
                    # a verbatim span of the seeded body_text ("def x(): ...")
                    quote="def x():",
                )
            ],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "passed"
    assert result.checks.L1_coverage is True


# ---------------------------------------------------------------------------
# L1: quote-substring guard (invariant 7, ADR-0011) — a quote must be a verbatim
# (whitespace-normalized) span of one of the claim's RESOLVABLE cited units.
# ---------------------------------------------------------------------------


async def test_l1_fabricated_quote_not_in_any_cited_unit_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The citation is real + retrieved (L0 passes) and the quote is within the cap,
    # but the quote text appears in NO cited unit ⇒ L1 fails with quote_not_found.
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="t",
                    evidence_ids=[str(evidence)],
                    # the unit body is "def x(): ..."; this is a wholly invented span
                    quote="raise SystemExit('fabricated')",
                )
            ],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    # L0 passes — the FABRICATION is caught only by L1's substring guard.
    assert result.checks.L0_exists is True
    assert result.checks.L0_in_requester_ledger is True
    assert result.checks.L1_coverage is False
    assert result.result == "failed"
    assert "quote_not_found" in result.failed_reasons


async def test_l1_verbatim_quote_passes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A quote that IS a verbatim span of a cited, retrieved unit ⇒ L1 passes.
    async with factory() as session:
        evidence = await insert_artifact(
            session,
            title="login",
            body_text="def login(user):\n    return hash_pw(user.password)",
            artifact_type="code_symbol",
            knowledge_kind="source_backed",
            path="auth.py",
        )
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login hashes the password",
                    evidence_ids=[str(evidence)],
                    quote="return hash_pw(user.password)",
                )
            ],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "passed"
    assert result.checks.L1_coverage is True
    assert "quote_not_found" not in result.failed_reasons


async def test_l1_quote_matches_under_whitespace_normalization(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The cited body wraps the span across newlines + extra spaces; the quote uses
    # single spaces. Whitespace-normalized, they match ⇒ L1 passes (never fuzzy).
    async with factory() as session:
        evidence = await insert_artifact(
            session,
            title="login",
            body_text="def login(user):\n        return  hash_pw(  user.password  )",
            artifact_type="code_symbol",
            knowledge_kind="source_backed",
            path="auth.py",
        )
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login hashes the password",
                    evidence_ids=[str(evidence)],
                    quote="return hash_pw( user.password )",
                )
            ],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "passed"
    assert result.checks.L1_coverage is True


async def test_l1_quote_in_unretrieved_unit_does_not_satisfy_guard(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The quote IS a verbatim span of a real, in-version, ACL-visible unit — but the
    # requester never retrieved it (no oracle, invariant 6). Coverage already fails
    # (claim_uncited); the guard must NOT rescue it, and the body must not be read.
    async with factory() as session:
        evidence = await insert_artifact(
            session,
            title="login",
            body_text="def login(user):\n    return hash_pw(user.password)",
            artifact_type="code_symbol",
            knowledge_kind="source_backed",
            path="auth.py",
        )
    # retrieved by ANOTHER subject only ⇒ not resolvable for this requester.
    await _record_retrieval(factory, [evidence], subject="other-agent")
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login hashes the password",
                    evidence_ids=[str(evidence)],
                    quote="return hash_pw(user.password)",
                )
            ],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "failed"
    assert result.checks.L1_coverage is False
    # The claim is uncited for this requester; the verbatim-in-DB span is irrelevant.
    assert "claim_uncited" in result.failed_reasons


async def test_l1_no_quote_claim_unaffected_by_guard(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A claim with NO quote is unaffected by the substring guard: it passes on
    # coverage alone (quote is optional).
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "passed"
    assert result.checks.L1_coverage is True
    assert "quote_not_found" not in result.failed_reasons


async def test_l1_over_cap_quote_still_fails_on_length(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # An over-cap quote fails on LENGTH (existing behavior) regardless of whether it
    # is grounded; the substring guard does not add a redundant second reason.
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient(), settings=BrokerSettings(max_quote_chars=4))

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="t",
                    evidence_ids=[str(evidence)],
                    # a verbatim span of the body but well over the 4-char cap
                    quote="def x(): ...",
                )
            ],
            verifier_levels=["L0", "L1"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "failed"
    assert result.checks.L1_coverage is False
    assert "quote_over_cap" in result.failed_reasons
    # over-cap is the only quote reason; the guard is skipped for an over-cap quote.
    assert "quote_not_found" not in result.failed_reasons


# ---------------------------------------------------------------------------
# L2: typed-fact adjudication (no LLM)
# ---------------------------------------------------------------------------


async def test_l2_symbol_in_file_matches_ledger_passes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login is defined in auth.py",
                    evidence_ids=[str(evidence)],
                    assertion=SymbolInFileAssertion(
                        kind="symbol_in_file", symbol="login", file="auth.py"
                    ),
                )
            ],
            verifier_levels=["L0", "L1", "L2"],
        ),
        REQUESTER,
    )

    assert receipt.verifier_levels_run == ["L0", "L1", "L2"]
    result = receipt.claim_results[0]
    assert result.result == "passed"
    assert result.checks.L2_typed_fact is True


async def test_l2_quote_present_but_assertion_false_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE headline case: the citation is real + retrieved (L0 passes), the quote
    is within cap (L1 passes), but the typed assertion misreads the evidence —
    ``login`` is in auth.py, not billing.py — so L2 fails the claim."""
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login is defined in billing.py",
                    evidence_ids=[str(evidence)],
                    quote="def x():",  # genuine span of the unit, within cap
                    assertion=SymbolInFileAssertion(
                        kind="symbol_in_file", symbol="login", file="billing.py"
                    ),
                )
            ],
            verifier_levels=["L0", "L1", "L2"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    # L0 + L1 both pass — the misread is ONLY caught by L2.
    assert result.checks.L0_exists is True
    assert result.checks.L0_in_requester_ledger is True
    assert result.checks.L1_coverage is True
    assert result.checks.L2_typed_fact is False
    assert result.result == "failed"
    assert "typed_fact_unsupported" in result.failed_reasons


async def test_l2_file_imports_module_matches_ledger(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        file_art = await insert_artifact(
            session,
            title="auth.py",
            body_text="",
            artifact_type="code_file",
            path="auth.py",
        )
        module_art = await insert_artifact(
            session, title="logging", body_text="", artifact_type="code_file"
        )
        await insert_edge(
            session,
            from_artifact_id=file_art,
            to_artifact_id=module_art,
            edge_type="imports",
            trust_class="EXTRACTED",
        )
    await _record_retrieval(factory, [file_art])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="auth.py imports logging",
                    evidence_ids=[str(file_art)],
                    assertion=FileImportsModuleAssertion(
                        kind="file_imports_module", file="auth.py", module="logging"
                    ),
                )
            ],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.checks.L2_typed_fact is True
    assert result.result == "passed"


async def test_l2_file_imports_module_wrong_module_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        file_art = await insert_artifact(
            session,
            title="auth.py",
            body_text="",
            artifact_type="code_file",
            path="auth.py",
        )
        module_art = await insert_artifact(
            session, title="logging", body_text="", artifact_type="code_file"
        )
        await insert_edge(
            session,
            from_artifact_id=file_art,
            to_artifact_id=module_art,
            edge_type="imports",
            trust_class="EXTRACTED",
        )
    await _record_retrieval(factory, [file_art])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="auth.py imports os",
                    evidence_ids=[str(file_art)],
                    assertion=FileImportsModuleAssertion(
                        kind="file_imports_module", file="auth.py", module="os"
                    ),
                )
            ],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.checks.L2_typed_fact is False
    assert result.result == "failed"


async def test_l2_edge_between_matches_either_direction(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        caller = await _symbol_artifact(session, name="login", path="auth.py")
        callee = await _symbol_artifact(session, name="hash_pw", path="crypto.py")
        await insert_edge(
            session,
            from_artifact_id=caller,
            to_artifact_id=callee,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
    await _record_retrieval(factory, [caller, callee])
    deps = make_broker_deps(factory, FakeSearchClient())

    # Assert the relation with endpoints swapped — existence, not orientation.
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login calls hash_pw",
                    # both endpoints must be cited, retrieved evidence (L2 only
                    # adjudicates over the claim's own cited units, not arbitrary ids)
                    evidence_ids=[str(caller), str(callee)],
                    assertion=EdgeBetweenAssertion(
                        kind="edge_between",
                        edge_type="calls",
                        from_id=str(callee),
                        to_id=str(caller),
                    ),
                )
            ],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    assert receipt.claim_results[0].checks.L2_typed_fact is True


# ---------------------------------------------------------------------------
# L2 is not an oracle: it adjudicates ONLY over the claim's cited, retrieved units
# ---------------------------------------------------------------------------


async def test_l2_assertion_over_unretrieved_unit_fails_closed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The ledger HAS a matching, ACL-visible, in-version unit (login in auth.py),
    # but the requester never retrieved it. L2 must NOT confirm the typed fact — that
    # would be an oracle over content the requester was never served (invariant 6).
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    # deliberately NO _record_retrieval for this requester.
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login is in auth.py",
                    evidence_ids=[str(evidence)],
                    assertion=SymbolInFileAssertion(
                        kind="symbol_in_file", symbol="login", file="auth.py"
                    ),
                )
            ],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    # The fact is true in the ledger, but unretrieved ⇒ L2 fails closed (no leak).
    assert receipt.claim_results[0].checks.L2_typed_fact is False


async def test_l2_edge_between_uncited_endpoint_fails(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # An edge exists between two retrieved symbols, but the claim cites only one of
    # them. The requester must not be able to probe whether an edge exists to an
    # endpoint they did not cite — both endpoints must be cited, retrieved evidence.
    async with factory() as session:
        caller = await _symbol_artifact(session, name="login", path="auth.py")
        callee = await _symbol_artifact(session, name="hash_pw", path="crypto.py")
        await insert_edge(
            session,
            from_artifact_id=caller,
            to_artifact_id=callee,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
    await _record_retrieval(factory, [caller, callee])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login calls hash_pw",
                    evidence_ids=[str(caller)],  # callee deliberately NOT cited
                    assertion=EdgeBetweenAssertion(
                        kind="edge_between",
                        edge_type="calls",
                        from_id=str(caller),
                        to_id=str(callee),
                    ),
                )
            ],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    assert receipt.claim_results[0].checks.L2_typed_fact is False


async def test_l2_edge_between_inferred_trust_does_not_support(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # An INFERRED_HIGH edge is a routing hint only (trust-buckets.md) — it must
    # not let L2 adjudicate a typed fact as true.
    async with factory() as session:
        a = await _symbol_artifact(session, name="a", path="a.py")
        b = await _symbol_artifact(session, name="b", path="b.py")
        await insert_edge(
            session,
            from_artifact_id=a,
            to_artifact_id=b,
            edge_type="documents",
            trust_class="INFERRED_HIGH",
        )
    await _record_retrieval(factory, [a, b])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="a documents b",
                    evidence_ids=[str(a)],
                    assertion=EdgeBetweenAssertion(
                        kind="edge_between", edge_type="documents", from_id=str(a), to_id=str(b)
                    ),
                )
            ],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    assert receipt.claim_results[0].checks.L2_typed_fact is False


async def test_l2_respects_acl_invisible_unit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The symbol unit is team-restricted; a teamless requester cannot see it, so
    # even a correct typed assertion finds no VISIBLE unit ⇒ L2 fails.
    async with factory() as session:
        evidence = await _symbol_artifact(
            session, name="login", path="auth.py", acl_teams=["secret-team"]
        )
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login is in auth.py",
                    evidence_ids=[str(evidence)],
                    assertion=SymbolInFileAssertion(
                        kind="symbol_in_file", symbol="login", file="auth.py"
                    ),
                )
            ],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    # ACL fails L0 and L2 both — the requester could not see the unit at all.
    assert result.checks.L0_acl_visible is False
    assert result.checks.L2_typed_fact is False
    assert result.result == "failed"


async def test_l2_no_assertion_is_not_adjudicated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A claim carrying no typed assertion gets NO L2 verdict (key stays absent),
    # so a valid L0 claim still passes under an L2 request.
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
            verifier_levels=["L0", "L2"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.checks.L2_typed_fact is None
    assert result.result == "passed"


async def test_verifier_levels_run_is_order_stable(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        evidence = await _symbol_artifact(session, name="login", path="auth.py")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient())

    # Request out of order ⇒ verifier_levels_run still canonical (L0, L1, L2).
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
            verifier_levels=["L2", "L1", "L0"],
        ),
        REQUESTER,
    )
    assert receipt.verifier_levels_run == ["L0", "L1", "L2"]
