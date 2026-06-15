"""Request/response schemas for context.verify_answer (ADR-0011).

The verifier is the trust boundary: an answer is platform-trusted iff it
carries a valid receipt (docs/contracts/verification-receipt.md). L0 (phase 1)
runs the deterministic, mandatory provenance checks. Phase 4 adds the
deterministic L1 (citation coverage + span caps) and L2 (typed-fact) levels;
both are additive — a phase-1 caller (no ``quote``/``assertion``, default
``verifier_levels=["L0"]``) is byte-for-byte unchanged. The receipt reserves
``client_id`` and ``signature`` (null) for phase-4 signing/identity and keeps
``verifier_levels_run`` and ``checks`` open so each level appends, never
restructures.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from agentic_mcp_server.mcp.tool_schemas.base import McpModel

#: Verifier levels available now. L3 (LLM entailment, cached) is PR-31: it runs
#: ONLY for claims L0-L2 could not adjudicate deterministically (cost guard).
VerifierLevel = Literal["L0", "L1", "L2", "L3"]

ClaimResult = Literal["passed", "failed"]
OverallResult = Literal["passed", "failed", "partial"]

#: Receipt schema is versioned independently of MCP_SCHEMA_VERSION: hosts pin to
#: the receipt shape, not the broker's wire version.
RECEIPT_SCHEMA_VERSION = 1


def _reject_control_chars(value: str, field_name: str) -> str:
    """Reject C0/DEL control chars in agent-supplied ids.

    ``answer_id`` and ``claim_id`` are untrusted agent input that the broker echoes
    into structured logs; a newline/CR would let an agent forge log lines. Reject at
    the schema boundary so no downstream log site has to sanitize.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


class SymbolInFileAssertion(McpModel):
    """ "symbol X is defined in file F" — adjudicated against an AST fact unit."""

    kind: Literal["symbol_in_file"]
    symbol: str = Field(min_length=1)
    file: str = Field(min_length=1)


class FileImportsModuleAssertion(McpModel):
    """ "file F imports module M" — adjudicated against an `imports` edge fact."""

    kind: Literal["file_imports_module"]
    file: str = Field(min_length=1)
    module: str = Field(min_length=1)


class EdgeBetweenAssertion(McpModel):
    """ "an edge of type T exists between A and B" — adjudicated against an edge fact."""

    kind: Literal["edge_between"]
    edge_type: str = Field(min_length=1)
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)


#: Discriminated by ``kind``; L2 adjudicates each against the ledger. Phase-4
#: callers may omit ``assertion`` entirely — L2 then skips the claim.
ClaimAssertion = SymbolInFileAssertion | FileImportsModuleAssertion | EdgeBetweenAssertion


class ClaimInput(McpModel):
    """One claim the agent asserts, with the evidence ids it cites."""

    claim_id: str = Field(min_length=1, max_length=128)
    text: str
    # A claim with no cited evidence cannot be provenance-checked: reject it at
    # the schema boundary (verification-receipt.md "reject ... empty evidence").
    evidence_ids: list[str] = Field(min_length=1)
    # Optional verbatim span the claim relies on; L1 caps its length.
    quote: str | None = None
    # Optional typed assertion the L2 verifier adjudicates against the ledger.
    assertion: ClaimAssertion | None = Field(default=None, discriminator="kind")

    @field_validator("claim_id")
    @classmethod
    def _claim_id_no_control(cls, value: str) -> str:
        return _reject_control_chars(value, "claim_id")


class VerifyAnswerRequest(McpModel):
    answer_id: str = Field(min_length=1, max_length=256)
    # At least one claim — an empty answer has nothing to verify.
    claims: list[ClaimInput] = Field(min_length=1)
    # null ⇒ the active/served graph_version.
    graph_version: str | None = None
    # Defaults to L0; request up to ["L0","L1","L2"]. Server runs per policy.
    verifier_levels: list[VerifierLevel] = Field(default_factory=lambda: ["L0"])

    @field_validator("answer_id")
    @classmethod
    def _answer_id_no_control(cls, value: str) -> str:
        return _reject_control_chars(value, "answer_id")

    @field_validator("verifier_levels")
    @classmethod
    def _at_least_one_level(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("verifier_levels must request at least one level (e.g. ['L0'])")
        return value


class ClaimChecks(McpModel):
    """Open object keyed by check name; each level appends keys, never edits prior ones.

    L0 keys are always present (L0 is mandatory). L1/L2 keys are present only when
    that level ran and produced a verdict for the claim — ``L2_typed_fact`` is
    omitted for a claim that carries no assertion (L2 cannot adjudicate it).
    """

    L0_exists: bool
    L0_in_active_version: bool
    L0_acl_visible: bool
    L0_in_requester_ledger: bool
    L0_not_stale: bool
    # Cited support is EXTRACTED, not an INFERRED routing hint.
    L0_supporting_trust_ok: bool
    # L1: claim cites ≥1 checkable unit and any quote is within the span cap.
    L1_coverage: bool | None = None
    # L2: the claim's typed assertion matches a deterministic ledger unit.
    L2_typed_fact: bool | None = None
    # L3 (PR-31): the cited evidence ENTAILS the claim (cached LLM check). Present
    # ONLY for a claim L0-L2 could not adjudicate deterministically AND that has
    # resolvable cited evidence; absent (None) otherwise — L3 never runs on an
    # L2-resolved claim (cost discipline).
    L3_entailment: bool | None = None


#: Backwards-compatible alias: L0-only callers/tests still import ``L0Checks``.
L0Checks = ClaimChecks


class ClaimReceipt(McpModel):
    claim_id: str
    result: ClaimResult
    checks: ClaimChecks
    failed_reasons: list[str] = Field(default_factory=list)


#: Outcome of the official-client platform-trust gate (ADR-0011 §6, phase 4). A
#: ``verification_required`` client is platform-trusted ONLY with a valid,
#: client-matched receipt; otherwise the broker returns a STRUCTURED denial (no
#: silent pass). A client that did not opt into ``verification_required`` is
#: ``not_required`` — its behaviour is unchanged.
PlatformTrustStatus = Literal["trusted", "denied", "not_required"]

#: Stable denial reason codes for the platform-trust gate (ids/outcomes only,
#: never answer/evidence text or any secret).
TRUST_REASON_NO_RECEIPT = "verification_required_no_receipt"
TRUST_REASON_UNSIGNED = "receipt_unsigned"
TRUST_REASON_BAD_SIGNATURE = "receipt_signature_invalid"
TRUST_REASON_CLIENT_MISMATCH = "receipt_client_mismatch"
TRUST_REASON_NOT_PASSED = "receipt_overall_not_passed"


class PlatformTrustDecision(McpModel):
    """The broker's official-client trust verdict for a client + (optional) receipt.

    ``status`` is ``trusted`` only when a ``verification_required`` client presents a
    valid, client-matched, passing receipt; ``denied`` (with ``reason``) otherwise;
    ``not_required`` for a client that did not opt in. ``client_id`` echoes the
    validated client the decision was made for.
    """

    status: PlatformTrustStatus
    client_id: str
    verification_required: bool
    reason: str | None = None


class VerificationReceipt(McpModel):
    receipt_schema_version: Literal[1] = RECEIPT_SCHEMA_VERSION
    # sha256 over the normalized claims (stable for the same normalized input).
    answer_hash: str
    graph_version: str = Field(min_length=1)
    issued_at: datetime
    verifier_levels_run: list[VerifierLevel]
    overall: OverallResult
    claim_results: list[ClaimReceipt]
    # The validated client identity this receipt was issued to (phase 4). Stamped
    # from the authenticated client credential, bound into the signed payload, and
    # used to scope the receipt: a receipt for client A does NOT validate for B.
    # Null when no client identity was resolved (e.g. an L0-only internal call).
    client_id: str | None = None
    # HMAC-SHA256 over (answer_hash + graph_version + claim_results); null until a
    # signing key is configured (PR-31). A host validates it statelessly via
    # context_broker.receipt_signing.verify_receipt_signature.
    signature: str | None = None
    # Non-secret fingerprint of the signing key (tells a host WHICH key signed,
    # never the key itself); null when the receipt is unsigned. Additive — a
    # phase-1 caller that ignores it is unaffected.
    key_id: str | None = None


class PlatformTrustRequest(McpModel):
    """Ask the broker whether the calling client's answer is platform-trusted.

    The client identity is taken from the authenticated session (never a request
    field). ``receipt`` is the receipt the client obtained from ``context.verify_answer``
    (omit it to test the gate with no receipt). The broker evaluates it against the
    calling client's ``verification_required`` policy and the receipt's client binding.
    """

    receipt: VerificationReceipt | None = None
