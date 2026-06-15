"""Official-client platform-trust gate (PR-32, ADR-0011 §6).

Pure, DB-free coverage: a verification_required client is platform-trusted ONLY with
a valid, client-matched, passing receipt; a structured denial (never a silent pass)
otherwise; a non-opted-in client is unchanged (not_required). A receipt for client A
must NOT trust client B (cross-client reuse blocked). No secret value reaches the logs.
"""

import logging
from datetime import UTC, datetime

import pytest

from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.context_broker.platform_trust import evaluate_platform_trust
from agentic_mcp_server.context_broker.receipt_signing import (
    DEFAULT_SIGNING_KEY_ENV,
    sign_receipt,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimChecks,
    ClaimReceipt,
    VerificationReceipt,
)

KEY = "unit-test-signing-key-not-a-secret"
ENV = DEFAULT_SIGNING_KEY_ENV


def _required(client_id: str) -> ClientIdentity:
    return ClientIdentity(client_id=client_id, verification_required=True, registered=True)


def _receipt(*, client_id: str | None, overall: str = "passed") -> VerificationReceipt:
    return VerificationReceipt(
        answer_hash="a" * 64,
        graph_version="kb-test",
        issued_at=datetime.now(UTC),
        verifier_levels_run=["L0"],
        overall=overall,  # type: ignore[arg-type]
        claim_results=[
            ClaimReceipt(
                claim_id="c1",
                result="passed" if overall == "passed" else "failed",
                checks=ClaimChecks(
                    L0_exists=True,
                    L0_in_active_version=True,
                    L0_acl_visible=True,
                    L0_in_requester_ledger=True,
                    L0_not_stale=True,
                    L0_supporting_trust_ok=True,
                ),
            )
        ],
        client_id=client_id,
    )


def _signed(client_id: str, monkeypatch: pytest.MonkeyPatch, **kw: object) -> VerificationReceipt:
    monkeypatch.setenv(ENV, KEY)
    return sign_receipt(_receipt(client_id=client_id, **kw))  # type: ignore[arg-type]


def test_non_opted_in_client_is_not_required() -> None:
    client = ClientIdentity(client_id="casual", registered=True)
    decision = evaluate_platform_trust(client, None, signing_key_env=ENV)
    assert decision.status == "not_required"
    assert decision.verification_required is False
    assert decision.reason is None


def test_verification_required_without_receipt_is_denied() -> None:
    decision = evaluate_platform_trust(_required("official"), None, signing_key_env=ENV)
    assert decision.status == "denied"
    assert decision.reason == "verification_required_no_receipt"


def test_verification_required_with_unsigned_receipt_is_denied() -> None:
    unsigned = _receipt(client_id="official")
    decision = evaluate_platform_trust(_required("official"), unsigned, signing_key_env=ENV)
    assert decision.status == "denied"
    assert decision.reason == "receipt_unsigned"


def test_valid_client_matched_passing_receipt_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = _signed("official", monkeypatch)
    decision = evaluate_platform_trust(_required("official"), signed, signing_key_env=ENV)
    assert decision.status == "trusted"
    assert decision.client_id == "official"
    assert decision.reason is None


def test_receipt_for_client_a_is_rejected_for_client_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A valid receipt issued to client A must NOT trust client B (cross-client reuse).
    signed_for_a = _signed("client-a", monkeypatch)
    decision = evaluate_platform_trust(_required("client-b"), signed_for_a, signing_key_env=ENV)
    assert decision.status == "denied"
    assert decision.reason == "receipt_client_mismatch"


def test_tampered_receipt_fails_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    signed = _signed("official", monkeypatch)
    claim = signed.claim_results[0]
    tampered = signed.model_copy(
        update={
            "claim_results": [
                claim.model_copy(
                    update={"checks": claim.checks.model_copy(update={"L0_not_stale": False})}
                )
            ]
        }
    )
    decision = evaluate_platform_trust(_required("official"), tampered, signing_key_env=ENV)
    assert decision.status == "denied"
    assert decision.reason == "receipt_signature_invalid"


def test_failing_receipt_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    signed = _signed("official", monkeypatch, overall="failed")
    decision = evaluate_platform_trust(_required("official"), signed, signing_key_env=ENV)
    assert decision.status == "denied"
    assert decision.reason == "receipt_overall_not_passed"


def test_no_signing_key_configured_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    signed = _signed("official", monkeypatch)
    # Key removed at gate time: a verification_required client cannot be validated.
    monkeypatch.delenv(ENV, raising=False)
    decision = evaluate_platform_trust(_required("official"), signed, signing_key_env=ENV)
    assert decision.status == "denied"
    assert decision.reason == "receipt_signature_invalid"


def test_signing_key_value_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    signed = _signed("official", monkeypatch)
    with caplog.at_level(logging.DEBUG):
        evaluate_platform_trust(_required("official"), signed, signing_key_env=ENV)
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert KEY not in blob
