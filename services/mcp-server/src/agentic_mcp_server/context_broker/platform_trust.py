"""Official-client platform-trust enforcement (ADR-0011 §6, phase 4).

The broker governs retrieval, not an agent's final answer. The only enforceable
boundary against agents we do not control is: *an answer is platform-trusted iff it
carries a valid receipt* — and a host can REQUIRE one per client. This module is that
gate.

For a client whose registry policy sets ``verification_required``, evidence/answers
are marked platform-trusted ONLY when accompanied by a valid, client-matched, passing
receipt; otherwise the broker returns a clear STRUCTURED denial (no silent pass).
For a client that did NOT opt into ``verification_required``, behaviour is unchanged:
the gate returns ``not_required`` and never blocks.

This is pure policy: it composes WITH (never replaces) the ACL + trust filters already
enforced on retrieval. Client scopes are ADDITIONAL to user-level ACLs. The gate reads
the receipt's bound ``client_id`` and signature only — no database, no LLM, no secret
literal (the signing key value is read from its configured env NAME at call time).
"""

import logging
import os

from agentic_mcp_server.auth.client_identity import ClientIdentity
from agentic_mcp_server.context_broker.receipt_signing import verify_receipt_signature
from agentic_mcp_server.mcp.tool_schemas.verification import (
    TRUST_REASON_BAD_SIGNATURE,
    TRUST_REASON_CLIENT_MISMATCH,
    TRUST_REASON_NO_RECEIPT,
    TRUST_REASON_NOT_PASSED,
    TRUST_REASON_UNSIGNED,
    PlatformTrustDecision,
    VerificationReceipt,
)

logger = logging.getLogger(__name__)


def evaluate_platform_trust(
    client: ClientIdentity,
    receipt: VerificationReceipt | None,
    *,
    signing_key_env: str,
) -> PlatformTrustDecision:
    """Decide whether ``client``'s answer is platform-trusted, given a ``receipt``.

    - A client without ``verification_required`` ⇒ ``not_required`` (unchanged behaviour).
    - A ``verification_required`` client ⇒ ``trusted`` ONLY when ``receipt`` is present,
      signed under the configured key, bound to THIS client, and ``overall == "passed"``;
      ``denied`` (with a stable ``reason``) otherwise.

    The signing key VALUE is read from ``signing_key_env`` at call time and never
    logged. Logs carry the client_id, status, and reason only — no secret, no answer
    or evidence text.
    """
    if not client.verification_required:
        decision = PlatformTrustDecision(
            status="not_required",
            client_id=client.client_id,
            verification_required=False,
        )
        return _logged(decision)

    if receipt is None:
        return _logged(_deny(client, TRUST_REASON_NO_RECEIPT))
    if receipt.signature is None:
        return _logged(_deny(client, TRUST_REASON_UNSIGNED))
    # The receipt must be bound to THIS client (cross-client reuse is rejected).
    if receipt.client_id != client.client_id:
        return _logged(_deny(client, TRUST_REASON_CLIENT_MISMATCH))

    key = os.environ.get(signing_key_env)
    # No configured key ⇒ the signature cannot be validated, so a verification_required
    # client cannot be trusted. Fail closed (never silently pass).
    if not key or not verify_receipt_signature(receipt, key, expected_client_id=client.client_id):
        return _logged(_deny(client, TRUST_REASON_BAD_SIGNATURE))

    if receipt.overall != "passed":
        return _logged(_deny(client, TRUST_REASON_NOT_PASSED))

    return _logged(
        PlatformTrustDecision(
            status="trusted",
            client_id=client.client_id,
            verification_required=True,
        )
    )


def _deny(client: ClientIdentity, reason: str) -> PlatformTrustDecision:
    return PlatformTrustDecision(
        status="denied",
        client_id=client.client_id,
        verification_required=True,
        reason=reason,
    )


def _logged(decision: PlatformTrustDecision) -> PlatformTrustDecision:
    logger.info(
        "event=platform_trust client_id=%s verification_required=%s status=%s reason=%s",
        decision.client_id,
        decision.verification_required,
        decision.status,
        decision.reason,
    )
    return decision
