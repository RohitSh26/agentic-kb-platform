"""Unit tests for HMAC-SHA256 receipt signing (PR-31).

Pure, DB-free coverage of the stateless signer/verifier: a signature validates
under the right key, tampering fails, the key VALUE never reaches the logs, and
``key_id`` is a stable non-secret fingerprint. The signing key is provided by the
test directly (no fixture stores a real secret).
"""

import logging
from datetime import UTC, datetime

import pytest

from agentic_mcp_server.context_broker.receipt_signing import (
    DEFAULT_SIGNING_KEY_ENV,
    compute_key_id,
    sign_receipt,
    verify_receipt_signature,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimChecks,
    ClaimReceipt,
    VerificationReceipt,
)

KEY = "unit-test-key-value-not-a-secret"


def _receipt() -> VerificationReceipt:
    return VerificationReceipt(
        answer_hash="a" * 64,
        graph_version="kb-test",
        issued_at=datetime.now(UTC),
        verifier_levels_run=["L0"],
        overall="passed",
        claim_results=[
            ClaimReceipt(
                claim_id="c1",
                result="passed",
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
    )


def test_sign_then_verify_roundtrips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_SIGNING_KEY_ENV, KEY)
    signed = sign_receipt(_receipt())
    assert signed.signature is not None
    assert signed.key_id == compute_key_id(KEY.encode("utf-8"))
    assert verify_receipt_signature(signed, KEY) is True


def test_wrong_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_SIGNING_KEY_ENV, KEY)
    signed = sign_receipt(_receipt())
    assert verify_receipt_signature(signed, "different-key") is False


def test_unsigned_receipt_never_validates() -> None:
    # A receipt with no signature is never platform-trusted.
    assert verify_receipt_signature(_receipt(), KEY) is False


def test_signature_independent_of_issued_at(monkeypatch: pytest.MonkeyPatch) -> None:
    # issued_at is deliberately NOT in the signed payload, so changing it does not
    # invalidate the signature (the host trusts answer_hash + graph_version + claims).
    monkeypatch.setenv(DEFAULT_SIGNING_KEY_ENV, KEY)
    signed = sign_receipt(_receipt())
    moved = signed.model_copy(update={"issued_at": datetime(2000, 1, 1, tzinfo=UTC)})
    assert verify_receipt_signature(moved, KEY) is True


def test_tampering_checks_breaks_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_SIGNING_KEY_ENV, KEY)
    signed = sign_receipt(_receipt())
    claim = signed.claim_results[0]
    flipped = claim.model_copy(
        update={"checks": claim.checks.model_copy(update={"L0_not_stale": False})}
    )
    tampered = signed.model_copy(update={"claim_results": [flipped]})
    assert verify_receipt_signature(tampered, KEY) is False


def test_missing_key_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_SIGNING_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError):
        sign_receipt(_receipt())


def test_key_value_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(DEFAULT_SIGNING_KEY_ENV, KEY)
    with caplog.at_level(logging.DEBUG):
        signed = sign_receipt(_receipt())
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert KEY not in blob
    # the key_id (non-secret) may appear; the key value must not.
    assert signed.key_id is not None
