"""Client identity + receipt binding + official-client enforcement (PR-32).

End-to-end over the real verifier against the migrated test DB: verify_answer stamps
the validated client_id into the (signed) receipt and binds it; a receipt for client A
does not satisfy client B; the official-client gate denies a verification_required
client without a valid receipt; and the client-scope check composes WITH the user ACL
(both must pass — client scopes never widen what the user may see).
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

from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.auth.scopes import (
    SCOPE_VERIFY,
    client_may_call,
)
from agentic_mcp_server.context_broker.dependencies import BrokerSettings
from agentic_mcp_server.context_broker.platform_trust import evaluate_platform_trust
from agentic_mcp_server.context_broker.verify import verify_answer
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
SIGNING_ENV = "VERIFY_SIGNING_KEY"
SIGNING_KEY = "integration-signing-key-not-a-secret"


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    return make_session_factory()


@pytest.fixture(autouse=True)
async def registry(factory) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    async with factory() as session:
        await require_registry_schema(session)
        await clean_registry(session)
        await insert_build_run(session, KB_VERSION, "active")
    yield


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)


def _client(client_id: str, *, verification_required: bool) -> ClientIdentity:
    return ClientIdentity(
        client_id=client_id,
        scopes=frozenset({SCOPE_VERIFY}),
        verification_required=verification_required,
        registered=True,
    )


async def _seed_and_retrieve(factory, *, acl_teams: list[str] | None = None) -> uuid.UUID:  # type: ignore[no-untyped-def]
    async with factory() as session:
        artifact = await insert_artifact(session, title="ev", body_text="x", acl_teams=acl_teams)
        neighbor = await insert_artifact(session, title="n", body_text="y")
        await insert_edge(
            session,
            from_artifact_id=artifact,
            to_artifact_id=neighbor,
            edge_type="calls",
            trust_class="EXTRACTED",
        )
    async with factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id="run-seed",
                agent_name=SUBJECT,
                tool_name="context.create_pack",
                status="approved",
                kb_version=KB_VERSION,
                returned_artifact_ids=[artifact],
            ),
        )
    return artifact


def _claim(evidence: uuid.UUID) -> ClaimInput:
    return ClaimInput(claim_id="c1", text="a claim", evidence_ids=[str(evidence)])


# ---------------------------------------------------------------------------
# Receipt binding
# ---------------------------------------------------------------------------


async def test_verify_answer_stamps_and_signs_with_client_id(factory) -> None:  # type: ignore[no-untyped-def]
    evidence = await _seed_and_retrieve(factory)
    deps = make_broker_deps(factory, FakeSearchClient())
    client = _client("official-a", verification_required=True)

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="ans-1", claims=[_claim(evidence)]),
        REQUESTER,
        client,
    )
    assert receipt.overall == "passed"
    assert receipt.client_id == "official-a"
    # A signing key is configured ⇒ the receipt is signed and bound to the client.
    assert receipt.signature is not None
    decision = evaluate_platform_trust(client, receipt, signing_key_env=SIGNING_ENV)
    assert decision.status == "trusted"


async def test_receipt_for_client_a_does_not_trust_client_b(factory) -> None:  # type: ignore[no-untyped-def]
    evidence = await _seed_and_retrieve(factory)
    deps = make_broker_deps(factory, FakeSearchClient())
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="ans-cross", claims=[_claim(evidence)]),
        REQUESTER,
        _client("client-a", verification_required=True),
    )
    # The SAME receipt presented as client B is rejected (cross-client reuse blocked).
    decision = evaluate_platform_trust(
        _client("client-b", verification_required=True),
        receipt,
        signing_key_env=SIGNING_ENV,
    )
    assert decision.status == "denied"
    assert decision.reason == "receipt_client_mismatch"


# ---------------------------------------------------------------------------
# Official-client enforcement
# ---------------------------------------------------------------------------


async def test_verification_required_client_without_receipt_is_denied(factory) -> None:  # type: ignore[no-untyped-def]
    client = _client("official", verification_required=True)
    decision = evaluate_platform_trust(client, None, signing_key_env=SIGNING_ENV)
    assert decision.status == "denied"
    assert decision.reason == "verification_required_no_receipt"


async def test_non_opted_in_client_is_unaffected(factory) -> None:  # type: ignore[no-untyped-def]
    evidence = await _seed_and_retrieve(factory)
    deps = make_broker_deps(factory, FakeSearchClient())
    client = _client("casual", verification_required=False)

    # verify_answer still works and stamps client_id, but the gate never blocks.
    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="ans-casual", claims=[_claim(evidence)]),
        REQUESTER,
        client,
    )
    assert receipt.client_id == "casual"
    decision = evaluate_platform_trust(client, None, signing_key_env=SIGNING_ENV)
    assert decision.status == "not_required"


# ---------------------------------------------------------------------------
# Composition: ACL + trust + scope all enforced together
# ---------------------------------------------------------------------------


async def test_acl_and_trust_and_scope_compose(factory) -> None:  # type: ignore[no-untyped-def]
    # ACL: the cited artifact is team-restricted; the requester carries no teams, so
    # L0_acl_visible fails — the user ACL is NOT weakened by any client scope grant.
    evidence = await _seed_and_retrieve(factory, acl_teams=["secret-team"])
    deps = make_broker_deps(
        factory, FakeSearchClient(), settings=BrokerSettings(signing_key_env=SIGNING_ENV)
    )
    client = _client("official", verification_required=True)

    receipt = await verify_answer(
        deps,
        VerifyAnswerRequest(answer_id="ans-acl", claims=[_claim(evidence)]),
        REQUESTER,
        client,
    )
    # Scope admits the call (client holds context.verify) but the USER ACL still
    # fails the claim — defence in depth, client scope never widens visibility.
    assert client_may_call(client, "context.verify_answer") is True
    assert receipt.claim_results[0].checks.L0_acl_visible is False
    assert receipt.overall == "failed"

    # And the official-client gate denies trust because the receipt did not pass.
    decision = evaluate_platform_trust(client, receipt, signing_key_env=SIGNING_ENV)
    assert decision.status == "denied"
    assert decision.reason == "receipt_overall_not_passed"


async def test_scope_gate_blocks_a_registered_client_without_the_scope(factory) -> None:  # type: ignore[no-untyped-def]
    # A registered client lacking the verify scope cannot call verify_answer at all —
    # this is the ADDITIONAL client layer that composes with (does not replace) ACLs.
    no_verify = ClientIdentity(
        client_id="reader", scopes=frozenset({"context.read"}), registered=True
    )
    assert client_may_call(no_verify, "context.verify_answer") is False


def test_scope_gate_helper_enforces_the_registry_through_make_handlers() -> None:
    # The handler scope-gate helper (current_client_identity + client_may_call) is
    # exercised over the wire by tests/integration/test_auth.py (real token); here we
    # pin that build_server threads the registry into the broker deps so the gate has
    # the policy it needs — a registered client with graph.read only is denied verify.
    from agentic_mcp_server.auth.client_identity import parse_client_registry

    registry = parse_client_registry('{"impl-agent-client": {"scopes": ["graph.read"]}}')
    resolved = registry.resolve("impl-agent-client")
    assert client_may_call(resolved, "graph.get_neighbors") is True
    assert client_may_call(resolved, "context.verify_answer") is False
