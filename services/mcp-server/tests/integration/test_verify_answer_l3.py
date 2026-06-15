"""context.verify_answer L3 (cached LLM entailment) + signed receipts, PR-31.

L3 is the only non-deterministic verifier level, so it is gated hard by cost
discipline (ADR-0011, verification-receipt.md): it runs ONLY for a claim L0-L2
could not adjudicate, never on an L2-resolved claim, and a cache hit makes ZERO
LLM calls. The entailment backend is the hermetic FakeEntailmentClient (no Ollama
in CI; live `gemma3:4b` is a manual follow-up). Signing is exercised here too: the
receipt carries a valid HMAC and a tampered receipt fails validation. No key VALUE
ever appears in a fixture — the test HMAC key is set via monkeypatched env only.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from broker_test_support import (
    KB_VERSION,
    clean_registry,
    insert_artifact,
    insert_build_run,
    make_broker_deps,
    require_registry_schema,
)
from mcp_test_support import TEST_DATABASE_URL, make_session_factory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerSettings
from agentic_mcp_server.context_broker.receipt_signing import verify_receipt_signature
from agentic_mcp_server.context_broker.verify import verify_answer
from agentic_mcp_server.infrastructure.entailment.fake import FakeEntailmentClient
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.infrastructure.search.search_client import FakeSearchClient
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimInput,
    SymbolInFileAssertion,
    VerifyAnswerRequest,
)

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="no test database configured (set TEST_DATABASE_URL)",
)

SUBJECT = "impl-agent"
REQUESTER = Requester(subject=SUBJECT, teams=frozenset())

# Env var NAME holding the test signing key (config). The VALUE is set via
# monkeypatch at runtime — it is NEVER written into a fixture or asserted on.
SIGNING_ENV = "VERIFY_SIGNING_KEY"


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
    factory: async_sessionmaker[AsyncSession], artifact_ids: list[uuid.UUID]
) -> None:
    async with factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id="run-seed",
                agent_name=SUBJECT,
                tool_name="context.create_pack",
                status="approved",
                kb_version=KB_VERSION,
                returned_artifact_ids=artifact_ids,
            ),
        )


async def _prose_artifact(factory: async_sessionmaker[AsyncSession], *, body: str) -> uuid.UUID:
    """An edgeless source-backed prose artifact (satisfies L0 trust on its own) so
    a test can isolate the L3 entailment verdict from L0/L1."""
    async with factory() as session:
        return await insert_artifact(
            session,
            title="design note",
            body_text=body,
            artifact_type="doc_chunk",
            knowledge_kind="source_backed",
        )


# ---------------------------------------------------------------------------
# L3 entailment verdicts
# ---------------------------------------------------------------------------


async def test_entailed_claim_passes_l3(factory: async_sessionmaker[AsyncSession]) -> None:
    evidence = await _prose_artifact(factory, body="The cache is keyed by content hash.")
    await _record_retrieval(factory, [evidence])
    entail = FakeEntailmentClient()
    claim_text = "Caching is keyed by the content hash."
    entail.seed(claim_text, entailed=True, reason="evidence states the cache key")
    deps = make_broker_deps(factory, FakeSearchClient(), entailment_client=entail)

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text=claim_text, evidence_ids=[str(evidence)])],
            verifier_levels=["L0", "L1", "L3"],
        ),
        REQUESTER,
    )

    assert receipt.verifier_levels_run == ["L0", "L1", "L3"]
    result = receipt.claim_results[0]
    assert result.checks.L3_entailment is True
    assert result.result == "passed"
    assert len(entail.calls) == 1


async def test_non_entailed_paraphrase_fails_l3(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await _prose_artifact(factory, body="The cache is keyed by content hash.")
    await _record_retrieval(factory, [evidence])
    entail = FakeEntailmentClient()
    claim_text = "The cache never expires and stores full documents."
    entail.seed(claim_text, entailed=False, reason="evidence does not support this")
    deps = make_broker_deps(factory, FakeSearchClient(), entailment_client=entail)

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text=claim_text, evidence_ids=[str(evidence)])],
            verifier_levels=["L0", "L3"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.checks.L3_entailment is False
    assert result.result == "failed"
    assert "entailment_unsupported" in result.failed_reasons


# ---------------------------------------------------------------------------
# Gating: L3 runs ONLY on deterministically-unresolved claims
# ---------------------------------------------------------------------------


async def test_l3_does_not_run_on_l2_resolved_claim(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A claim carrying a typed assertion L2 RESOLVED (here: passes) must NOT be sent
    to the LLM — cost discipline. We assert the entailment client was never called
    and L3_entailment stays absent for it."""
    async with factory() as session:
        symbol = await insert_artifact(
            session,
            title="login",
            body_text="def login(): ...",
            artifact_type="code_symbol",
            knowledge_kind="source_backed",
            path="auth.py",
            span_start=10,
            span_end=20,
        )
    await _record_retrieval(factory, [symbol])
    entail = FakeEntailmentClient()
    deps = make_broker_deps(factory, FakeSearchClient(), entailment_client=entail)

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[
                ClaimInput(
                    claim_id="c1",
                    text="login is in auth.py",
                    evidence_ids=[str(symbol)],
                    assertion=SymbolInFileAssertion(
                        kind="symbol_in_file", symbol="login", file="auth.py"
                    ),
                )
            ],
            verifier_levels=["L0", "L2", "L3"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    # L2 settled the claim; L3 never ran for it.
    assert result.checks.L2_typed_fact is True
    assert result.checks.L3_entailment is None
    assert entail.calls == []


async def test_l3_skips_claim_already_failing_l1(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The claim cites evidence retrieved by NOBODY for this requester ⇒ fails L1
    # coverage. A failing claim is not piled onto by the LLM — L3 must not run.
    async with factory() as session:
        evidence = await insert_artifact(
            session, title="note", body_text="x", artifact_type="doc_chunk"
        )
    # deliberately NO retrieval for this requester.
    entail = FakeEntailmentClient()
    deps = make_broker_deps(factory, FakeSearchClient(), entailment_client=entail)

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
            verifier_levels=["L0", "L1", "L3"],
        ),
        REQUESTER,
    )

    result = receipt.claim_results[0]
    assert result.result == "failed"
    assert result.checks.L3_entailment is None
    assert entail.calls == []


async def test_l3_dropped_when_no_entailment_client(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # L3 requested but no backend configured: the platform drops it (cost guard).
    evidence = await _prose_artifact(factory, body="body")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(factory, FakeSearchClient(), entailment_client=None)

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
            verifier_levels=["L0", "L3"],
        ),
        REQUESTER,
    )

    assert receipt.verifier_levels_run == ["L0"]
    assert receipt.claim_results[0].checks.L3_entailment is None


# ---------------------------------------------------------------------------
# Cache: a hit makes ZERO LLM calls
# ---------------------------------------------------------------------------


async def test_l3_cache_hit_makes_zero_llm_calls(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    evidence = await _prose_artifact(factory, body="The cache is keyed by content hash.")
    await _record_retrieval(factory, [evidence])
    entail = FakeEntailmentClient()
    claim_text = "Caching is keyed by the content hash."
    entail.seed(claim_text, entailed=True, reason="r")
    deps = make_broker_deps(factory, FakeSearchClient(), entailment_client=entail)

    request = VerifyAnswerRequest(
        answer_id="a",
        claims=[ClaimInput(claim_id="c1", text=claim_text, evidence_ids=[str(evidence)])],
        verifier_levels=["L0", "L3"],
    )

    first = await verify_answer(deps, request, REQUESTER)
    assert first.claim_results[0].checks.L3_entailment is True
    assert len(entail.calls) == 1  # miss ⇒ one LLM call

    # Re-verify the SAME claim+evidence: cache hit ⇒ NO additional LLM call.
    second = await verify_answer(deps, request, REQUESTER)
    assert second.claim_results[0].checks.L3_entailment is True
    assert len(entail.calls) == 1  # still one — the cache served the second run


# ---------------------------------------------------------------------------
# Signed receipts
# ---------------------------------------------------------------------------


async def test_receipt_signature_validates(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key VALUE is injected via env at runtime — never literalised in a fixture.
    key_value = "unit-test-signing-key-not-a-real-secret"
    monkeypatch.setenv(SIGNING_ENV, key_value)
    evidence = await _prose_artifact(factory, body="body")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(
        factory, FakeSearchClient(), settings=BrokerSettings(signing_key_env=SIGNING_ENV)
    )

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
        ),
        REQUESTER,
    )

    assert receipt.signature is not None
    assert receipt.key_id is not None
    # Stateless host-side validation succeeds with the same key.
    assert verify_receipt_signature(receipt, key_value) is True
    # A wrong key fails.
    assert verify_receipt_signature(receipt, "some-other-key") is False


async def test_tampered_answer_hash_fails_validation(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    key_value = "unit-test-signing-key-not-a-real-secret"
    monkeypatch.setenv(SIGNING_ENV, key_value)
    evidence = await _prose_artifact(factory, body="body")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(
        factory, FakeSearchClient(), settings=BrokerSettings(signing_key_env=SIGNING_ENV)
    )

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
        ),
        REQUESTER,
    )

    # Tamper with the answer_hash — the signature was over the original payload.
    tampered = receipt.model_copy(update={"answer_hash": "0" * 64})
    assert verify_receipt_signature(tampered, key_value) is False


async def test_tampered_claim_results_fails_validation(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    key_value = "unit-test-signing-key-not-a-real-secret"
    monkeypatch.setenv(SIGNING_ENV, key_value)
    evidence = await _prose_artifact(factory, body="body")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(
        factory, FakeSearchClient(), settings=BrokerSettings(signing_key_env=SIGNING_ENV)
    )

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
        ),
        REQUESTER,
    )

    # Flip a claim result from passed -> failed: the signed payload no longer matches.
    original = receipt.claim_results[0]
    flipped = original.model_copy(update={"result": "failed"})
    tampered = receipt.model_copy(update={"claim_results": [flipped]})
    assert verify_receipt_signature(tampered, key_value) is False


async def test_unsigned_receipt_when_key_unset(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No signing key configured ⇒ an UNSIGNED receipt is still issued (L0 floor).
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    evidence = await _prose_artifact(factory, body="body")
    await _record_retrieval(factory, [evidence])
    deps = make_broker_deps(
        factory, FakeSearchClient(), settings=BrokerSettings(signing_key_env=SIGNING_ENV)
    )

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(
            answer_id="a",
            claims=[ClaimInput(claim_id="c1", text="t", evidence_ids=[str(evidence)])],
        ),
        REQUESTER,
    )

    assert receipt.signature is None
    assert receipt.key_id is None
    assert receipt.overall == "passed"
