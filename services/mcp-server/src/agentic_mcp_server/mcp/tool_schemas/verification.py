"""Request/response schemas for context.verify_answer (ADR-0011, phase 1).

The verifier is the trust boundary: an answer is platform-trusted iff it
carries a valid receipt (docs/contracts/verification-receipt.md). Phase 1 runs
the deterministic, mandatory L0 provenance checks only; the receipt reserves
``client_id`` and ``signature`` (null) and keeps ``verifier_levels_run`` and
``checks`` open so phase-4 (L1-L3, signing, client identity) is purely additive.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from agentic_mcp_server.mcp.tool_schemas.base import McpModel

#: Phase-1 vocabulary; phase 4 appends without restructuring.
VerifierLevel = Literal["L0"]

ClaimResult = Literal["passed", "failed"]
OverallResult = Literal["passed", "failed", "partial"]

#: Receipt schema is versioned independently of MCP_SCHEMA_VERSION: hosts pin to
#: the receipt shape, not the broker's wire version.
RECEIPT_SCHEMA_VERSION = 1


class ClaimInput(McpModel):
    """One claim the agent asserts, with the evidence ids it cites."""

    claim_id: str = Field(min_length=1, max_length=128)
    text: str
    # A claim with no cited evidence cannot be provenance-checked: reject it at
    # the schema boundary (verification-receipt.md "reject ... empty evidence").
    evidence_ids: list[str] = Field(min_length=1)


class VerifyAnswerRequest(McpModel):
    answer_id: str = Field(min_length=1, max_length=256)
    # At least one claim — an empty answer has nothing to verify.
    claims: list[ClaimInput] = Field(min_length=1)
    # null ⇒ the active/served graph_version.
    graph_version: str | None = None
    # Phase 1: L0 only. The server may run fewer/more per policy.
    verifier_levels: list[VerifierLevel] = Field(default_factory=lambda: ["L0"])

    @field_validator("verifier_levels")
    @classmethod
    def _at_least_one_level(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("verifier_levels must request at least one level (phase 1: ['L0'])")
        return value


class L0Checks(McpModel):
    """Open object keyed by check name; L1-L3 add keys without changing these."""

    L0_exists: bool
    L0_in_active_version: bool
    L0_acl_visible: bool
    L0_in_requester_ledger: bool
    L0_not_stale: bool
    # Cited support is EXTRACTED, not an INFERRED routing hint.
    L0_supporting_trust_ok: bool


class ClaimReceipt(McpModel):
    claim_id: str
    result: ClaimResult
    checks: L0Checks
    failed_reasons: list[str] = Field(default_factory=list)


class VerificationReceipt(McpModel):
    receipt_schema_version: Literal[1] = RECEIPT_SCHEMA_VERSION
    # sha256 over the normalized claims (stable for the same normalized input).
    answer_hash: str
    graph_version: str = Field(min_length=1)
    issued_at: datetime
    verifier_levels_run: list[VerifierLevel]
    overall: OverallResult
    claim_results: list[ClaimReceipt]
    # Reserved for phase-4 client identity + signing; null in phase 1.
    client_id: str | None = None
    signature: str | None = None
