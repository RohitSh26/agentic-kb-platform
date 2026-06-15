"""context.verify_answer: deterministic L0/L1/L2 verifier (ADR-0011).

The broker governs retrieval, not the agent's answer; the only enforceable
trust boundary is "an answer is platform-trusted iff it carries a valid
receipt" (docs/contracts/verification-receipt.md). The mandatory, deterministic
L0 checks run per cited evidence id:

  exists · in active version · ACL-visible to requester · in requester's
  retrieval ledger · not stale · supporting trust is EXTRACTED.

Phase 4 adds two more deterministic levels, run only when requested (additive —
an L0-only caller is unchanged):

  L1 (coverage)   — the claim cites ≥1 resolvable ledger unit and any quote it
                    carries is within the configured span cap.
  L2 (typed fact) — the claim's optional typed assertion (symbol-in-file,
                    file-imports-module, edge-between) matches a ledger unit;
                    a real-but-misread citation fails here where L0 passes.

A claim's ``result`` is the AND of every level that ran and produced a verdict
for it. ``overall`` is ``passed`` iff all claims passed, ``failed`` iff all
failed, else ``partial``.

The verifier performs NO generation/LLM and treats answer/evidence text as
untrusted: it never logs answer or evidence text — only ids, hashes, and check
outcomes. Every call writes a retrieval_event (verification is a broker action).
"""

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastmcp.exceptions import ToolError

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.claim_ledger import adjudicate_typed_fact
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import write_error_event
from agentic_mcp_server.context_broker.trust import CLAIM_SUPPORTING
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_version
from agentic_mcp_server.infrastructure.postgres.provenance import (
    ProvenanceRow,
    fetch_existing_anywhere,
    fetch_provenance,
)
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    fetch_subject_retrieved_ids,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.verification import (
    ClaimChecks,
    ClaimInput,
    ClaimReceipt,
    VerificationReceipt,
    VerifierLevel,
    VerifyAnswerRequest,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "context.verify_answer"
# Verification is not run-scoped (it carries an answer_id, not a run_id), so it
# uses the same non-run ledger sentinel as graph lookups.
NO_RUN_SENTINEL = "-"

# Stable failed_reason codes (ids/outcomes only — never answer/evidence text).
REASON_NOT_FOUND = "evidence_not_found"
REASON_WRONG_VERSION = "evidence_from_another_version"
REASON_ACL_INVISIBLE = "evidence_acl_invisible"
REASON_NOT_RETRIEVED = "evidence_not_retrieved_by_requester"
REASON_STALE = "evidence_stale"
REASON_TRUST = "evidence_supported_only_by_inferred_edge"
REASON_BAD_ID = "evidence_id_not_a_valid_artifact_id"
# L1 (phase 4): coverage + span cap.
REASON_UNCITED = "claim_uncited"
REASON_QUOTE_OVER_CAP = "quote_over_cap"
# L2 (phase 4): typed-fact adjudication.
REASON_TYPED_FACT_UNSUPPORTED = "typed_fact_unsupported"


def _normalized_answer_hash(request: VerifyAnswerRequest) -> str:
    """sha256 over the normalized claims — stable for the same normalized input.

    Normalization: per-claim ``text`` is whitespace-stripped, ``evidence_ids``
    are de-duplicated and sorted, and claims are sorted by ``claim_id`` so claim
    ordering does not change the hash. The canonical form is compact JSON.
    """
    normalized = sorted(
        (
            {
                "claim_id": claim.claim_id,
                "text": " ".join(claim.text.split()),
                "evidence_ids": sorted(set(claim.evidence_ids)),
            }
            for claim in request.claims
        ),
        key=lambda c: c["claim_id"],
    )
    canonical = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _EvidenceContext:
    """Everything L0 needs, resolved once per request before per-claim checks."""

    in_version: dict[uuid.UUID, ProvenanceRow]
    exists_anywhere: set[uuid.UUID]
    acl_visible: set[uuid.UUID]
    retrieved_by_requester: set[uuid.UUID]


@dataclass(frozen=True)
class _L0Result:
    """The six L0 bools for one cited evidence id (claim-level checks AND these)."""

    exists: bool
    in_active_version: bool
    acl_visible: bool
    in_requester_ledger: bool
    not_stale: bool
    supporting_trust_ok: bool

    @property
    def resolvable(self) -> bool:
        """A unit L1 can count toward coverage: a real, in-version, visible,
        requester-retrieved ledger unit (the staleness/trust verdicts are L0's
        concern, not whether the citation resolves to a unit at all)."""
        return (
            self.exists and self.in_active_version and self.acl_visible and self.in_requester_ledger
        )


def _check_evidence(raw_id: str, ctx: _EvidenceContext) -> tuple[_L0Result, list[str]]:
    """Run the six L0 checks for one cited evidence id; return checks + reasons."""
    reasons: list[str] = []
    try:
        artifact_id = uuid.UUID(raw_id)
    except ValueError:
        # A malformed id cannot reference any artifact: every check fails.
        return (
            _L0Result(
                exists=False,
                in_active_version=False,
                acl_visible=False,
                in_requester_ledger=False,
                not_stale=False,
                supporting_trust_ok=False,
            ),
            [REASON_BAD_ID],
        )

    row = ctx.in_version.get(artifact_id)
    in_active_version = row is not None
    # Exists anywhere OR in the active version — distinguishes "wrong version"
    # from "does not exist at all" without leaking the other version's name.
    exists = in_active_version or artifact_id in ctx.exists_anywhere
    acl_visible = artifact_id in ctx.acl_visible
    in_ledger = artifact_id in ctx.retrieved_by_requester
    # Staleness and trust are only knowable for an in-version row; absent one we
    # cannot assert them, so they fail closed.
    not_stale = in_active_version and not row.source_is_deleted  # type: ignore[union-attr]
    # Claim support fails only when the evidence is reached SOLELY through inferred
    # edges. Standalone source-backed evidence (no incident edges — e.g. a summary)
    # and evidence with any EXTRACTED edge both qualify; an artifact whose only
    # incident edges are inferred does not (ADR-0011, verification-receipt.md L0).
    supporting_trust_ok = in_active_version and (
        row.has_extracted_edge or not row.has_any_edge  # type: ignore[union-attr]
    )

    if not exists:
        reasons.append(REASON_NOT_FOUND)
    elif not in_active_version:
        reasons.append(REASON_WRONG_VERSION)
    if not acl_visible:
        reasons.append(REASON_ACL_INVISIBLE)
    if not in_ledger:
        reasons.append(REASON_NOT_RETRIEVED)
    # Only report stale/trust when the row is present; otherwise the not-found /
    # wrong-version reason already explains the failure (avoid double-counting).
    if in_active_version and not not_stale:
        reasons.append(REASON_STALE)
    if in_active_version and not supporting_trust_ok:
        reasons.append(REASON_TRUST)

    result = _L0Result(
        exists=exists,
        in_active_version=in_active_version,
        acl_visible=acl_visible,
        in_requester_ledger=in_ledger,
        not_stale=not_stale,
        supporting_trust_ok=supporting_trust_ok,
    )
    return result, reasons


def _merge_l0(into: _L0Result, other: _L0Result) -> _L0Result:
    """A claim's per-claim L0 checks are the AND of every cited evidence's checks."""
    return _L0Result(
        exists=into.exists and other.exists,
        in_active_version=into.in_active_version and other.in_active_version,
        acl_visible=into.acl_visible and other.acl_visible,
        in_requester_ledger=into.in_requester_ledger and other.in_requester_ledger,
        not_stale=into.not_stale and other.not_stale,
        supporting_trust_ok=into.supporting_trust_ok and other.supporting_trust_ok,
    )


def _resolve_levels(requested: list[VerifierLevel]) -> list[VerifierLevel]:
    """Levels actually run: L0 is mandatory; L1/L2 run iff requested + supported.

    Order is fixed (L0, L1, L2) so verifier_levels_run is stable regardless of
    request ordering. A requested level the server does not support is dropped.
    """
    requested_set = set(requested)
    levels: list[VerifierLevel] = ["L0"]  # always run; the trust floor.
    levels.extend(lv for lv in ("L1", "L2") if lv in requested_set)
    return levels


def _l1_coverage(
    claim: ClaimInput, evidence: list[_L0Result], *, max_quote_chars: int
) -> tuple[bool, list[str]]:
    """L1: ≥1 cited evidence resolves to a unit AND any quote is within the cap."""
    reasons: list[str] = []
    cited = any(unit.resolvable for unit in evidence)
    if not cited:
        reasons.append(REASON_UNCITED)
    quote_ok = claim.quote is None or len(claim.quote) <= max_quote_chars
    if not quote_ok:
        reasons.append(REASON_QUOTE_OVER_CAP)
    return (cited and quote_ok), reasons


async def verify_answer(
    deps: BrokerDeps, request: VerifyAnswerRequest, requester: Requester
) -> VerificationReceipt:
    started = time.monotonic()
    answer_hash = _normalized_answer_hash(request)
    levels = _resolve_levels(request.verifier_levels)
    run_l1 = "L1" in levels
    run_l2 = "L2" in levels

    async with deps.session_factory() as session:
        active = await fetch_active_version(session)
        if active is None:
            await write_error_event(
                deps,
                tool_name=_TOOL_NAME,
                subject=requester.subject,
                query_text=request.answer_id,
            )
            raise ToolError("no active kb_version; the knowledge base has not been built yet")
        active_version = active.kb_version

        # null graph_version ⇒ active; a pinned version must equal the served
        # one (we serve exactly the last successful active version, invariant 5).
        graph_version = request.graph_version or active_version
        is_active = graph_version == active_version

        # Resolve every cited id once, then run pure per-claim checks over it.
        cited_ids: list[uuid.UUID] = []
        for claim in request.claims:
            for raw in claim.evidence_ids:
                try:
                    cited_ids.append(uuid.UUID(raw))
                except ValueError:
                    continue
        unique_ids = list(dict.fromkeys(cited_ids))

        if is_active:
            in_version = await fetch_provenance(
                session, unique_ids, active.build_seq, extracted_bucket=CLAIM_SUPPORTING
            )
        else:
            # A pinned non-active version is, by construction, not the served
            # one: nothing belongs to the active version under L0's contract.
            in_version = {}
        exists_anywhere = await fetch_existing_anywhere(session, unique_ids)
        retrieved = await fetch_subject_retrieved_ids(session, requester.subject)

        # L2 adjudicates each claim's typed assertion against the ledger in the
        # same session. Only over the active version (L0 already fails a pinned
        # non-active citation; the ledger reads the served build_seq).
        l2_verdicts: dict[str, bool] = {}
        if run_l2 and is_active:
            for claim in request.claims:
                if claim.assertion is not None:
                    l2_verdicts[claim.claim_id] = await adjudicate_typed_fact(
                        session,
                        claim.assertion,
                        build_seq=active.build_seq,
                        requester=requester,
                    )

    # ACL visibility reuses the same authorization policy as retrieval: an
    # in-version row is visible iff the policy admits its (acl_teams) artifact.
    acl_visible = {
        artifact_id
        for artifact_id, row in in_version.items()
        if not row.acl_teams or requester.teams.intersection(row.acl_teams)
    }

    ctx = _EvidenceContext(
        in_version=in_version,
        exists_anywhere=exists_anywhere,
        acl_visible=acl_visible,
        retrieved_by_requester=retrieved,
    )

    claim_results: list[ClaimReceipt] = []
    for claim in request.claims:
        merged: _L0Result | None = None
        evidence_results: list[_L0Result] = []
        reasons: list[str] = []
        for raw_id in claim.evidence_ids:
            unit, evidence_reasons = _check_evidence(raw_id, ctx)
            evidence_results.append(unit)
            merged = unit if merged is None else _merge_l0(merged, unit)
            reasons.extend(evidence_reasons)
        # merged is never None: the schema rejects a claim with empty evidence.
        assert merged is not None

        l0_passed = (
            merged.exists
            and merged.in_active_version
            and merged.acl_visible
            and merged.in_requester_ledger
            and merged.not_stale
            and merged.supporting_trust_ok
        )
        # A claim's result is the AND of every level that ran with a verdict.
        passed = l0_passed

        l1_coverage: bool | None = None
        if run_l1:
            l1_coverage, l1_reasons = _l1_coverage(
                claim, evidence_results, max_quote_chars=deps.settings.max_quote_chars
            )
            reasons.extend(l1_reasons)
            passed = passed and l1_coverage

        # L2 only yields a verdict for claims carrying a typed assertion; for the
        # rest the key stays absent (the verifier never invents an L2 verdict).
        l2_typed_fact: bool | None = None
        if run_l2 and claim.claim_id in l2_verdicts:
            l2_typed_fact = l2_verdicts[claim.claim_id]
            if not l2_typed_fact:
                reasons.append(REASON_TYPED_FACT_UNSUPPORTED)
            passed = passed and l2_typed_fact

        checks = ClaimChecks(
            L0_exists=merged.exists,
            L0_in_active_version=merged.in_active_version,
            L0_acl_visible=merged.acl_visible,
            L0_in_requester_ledger=merged.in_requester_ledger,
            L0_not_stale=merged.not_stale,
            L0_supporting_trust_ok=merged.supporting_trust_ok,
            L1_coverage=l1_coverage,
            L2_typed_fact=l2_typed_fact,
        )
        claim_results.append(
            ClaimReceipt(
                claim_id=claim.claim_id,
                result="passed" if passed else "failed",
                checks=checks,
                # De-duplicate reasons while preserving first-seen order.
                failed_reasons=list(dict.fromkeys(reasons)),
            )
        )

    passed_count = sum(1 for r in claim_results if r.result == "passed")
    if passed_count == len(claim_results):
        overall = "passed"
    elif passed_count == 0:
        overall = "failed"
    else:
        overall = "partial"

    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=NO_RUN_SENTINEL,
                agent_name=requester.subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=graph_version,
                # answer_id + hash only — never answer or evidence text.
                query_text=request.answer_id,
                normalized_query=answer_hash,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )

    logger.info(
        "broker.verify_answer answer_id=%s subject=%s graph_version=%s claims=%d "
        "levels=%s overall=%s passed=%d",
        request.answer_id,
        requester.subject,
        graph_version,
        len(claim_results),
        ",".join(levels),
        overall,
        passed_count,
    )

    return VerificationReceipt(
        answer_hash=answer_hash,
        graph_version=graph_version,
        issued_at=datetime.now(UTC),
        verifier_levels_run=levels,
        overall=overall,
        claim_results=claim_results,
        client_id=None,
        signature=None,
    )
