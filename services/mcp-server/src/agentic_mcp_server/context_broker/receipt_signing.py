"""Signed verification receipts (ADR-0011 phase 4, PR-31).

The receipt is the verifier's trust boundary: an answer is platform-trusted iff it
carries a VALID receipt (verification-receipt.md). Signing turns the receipt into a
host-checkable artifact — a host can validate one STATELESSLY (``verify_receipt_signature``)
without re-running any check or touching the database, and a tampered ``answer_hash``
or ``claim_results`` makes the signature mismatch.

Key handling (CLAUDE.md: no secrets in code/fixtures/logs):
- The signing key is read from an ENV VAR whose NAME is config (default
  ``VERIFY_SIGNING_KEY``). The NAME may appear in code/logs; the VALUE never does.
- ``key_id`` is a non-secret, stable fingerprint (a truncated HMAC of a fixed
  label under the key) so a host can tell WHICH key signed without exposing it.

Canonicalisation: the signed payload is a deterministic, compact JSON object over
exactly ``answer_hash`` + ``graph_version`` + ``claim_results`` (each claim reduced
to id, result, and its check booleans). Ordering is fixed so the same receipt always
hashes the same; any change to those fields changes the MAC.
"""

import hashlib
import hmac
import json
import logging
import os

from agentic_mcp_server.mcp.tool_schemas.verification import VerificationReceipt

logger = logging.getLogger(__name__)

#: Env var NAME holding the signing key value (the name is config; the value is
#: read at runtime and NEVER literalised in code/fixtures/logs).
DEFAULT_SIGNING_KEY_ENV = "VERIFY_SIGNING_KEY"

#: Fixed label the key_id fingerprint is computed over (a non-secret derivation).
_KEY_ID_LABEL = b"agentic-kb:verify-receipt:key-id:v1"


def _canonical_payload(receipt: VerificationReceipt) -> bytes:
    """Deterministic bytes over answer_hash + graph_version + claim_results.

    Only the fields a host must be able to trust are signed: the answer hash, the
    served graph version, and each claim's id/result/check booleans. ``issued_at``,
    ``signature``, and ``key_id`` are deliberately excluded (signing them would be
    circular / non-deterministic). Compact, key-sorted JSON makes the encoding
    stable across processes.
    """
    payload = {
        "answer_hash": receipt.answer_hash,
        "graph_version": receipt.graph_version,
        "claim_results": [
            {
                "claim_id": claim.claim_id,
                "result": claim.result,
                "checks": claim.checks.model_dump(),
            }
            for claim in receipt.claim_results
        ],
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return canonical.encode("utf-8")


def compute_key_id(key: bytes) -> str:
    """Non-secret, stable fingerprint of the signing key (HMAC of a fixed label).

    Lets a host record/route WHICH key signed without ever exposing the key: it is
    a one-way derivation, truncated, and carries no recoverable key material.
    """
    return hmac.new(key, _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]


def _load_key(env_var: str) -> bytes:
    value = os.environ.get(env_var)
    if not value:
        raise RuntimeError(
            f"{env_var} is unset — the verifier cannot sign receipts without a signing key"
        )
    return value.encode("utf-8")


def sign_receipt(
    receipt: VerificationReceipt, *, env_var: str = DEFAULT_SIGNING_KEY_ENV
) -> VerificationReceipt:
    """Return a copy of ``receipt`` with ``signature`` + ``client_id``-independent
    ``key_id`` populated. The key value is read from ``env_var`` at call time.

    The MAC is HMAC-SHA256 over the canonical payload. Logs carry the key_id and
    the answer_hash only — never the key value (no secret in logs).
    """
    key = _load_key(env_var)
    mac = hmac.new(key, _canonical_payload(receipt), hashlib.sha256).hexdigest()
    key_id = compute_key_id(key)
    logger.info(
        "event=receipt_signed answer_hash=%s key_id=%s claims=%d",
        receipt.answer_hash,
        key_id,
        len(receipt.claim_results),
    )
    return receipt.model_copy(update={"signature": mac, "key_id": key_id})


def verify_receipt_signature(receipt: VerificationReceipt, key: str | bytes) -> bool:
    """Stateless: True iff ``receipt.signature`` is a valid MAC over its canonical
    payload under ``key``. No database, no re-running of checks.

    A host calls this with the same key (resolved from ITS configured env/Key Vault
    name) to decide whether to treat the answer as platform-trusted. A tampered
    ``answer_hash`` / ``graph_version`` / ``claim_results`` changes the payload and
    fails the constant-time MAC comparison.
    """
    if receipt.signature is None:
        return False
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    expected = hmac.new(key_bytes, _canonical_payload(receipt), hashlib.sha256).hexdigest()
    # Constant-time compare so a failed validation does not leak timing.
    return hmac.compare_digest(expected, receipt.signature)


__all__ = [
    "DEFAULT_SIGNING_KEY_ENV",
    "compute_key_id",
    "sign_receipt",
    "verify_receipt_signature",
]
