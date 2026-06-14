"""context.verify_answer: deterministic L0 provenance verifier (ADR-0011).

The broker governs retrieval, not the agent's answer; the only enforceable
trust boundary is "an answer is platform-trusted iff it carries a valid
receipt" (docs/contracts/verification-receipt.md). Phase 1 ships the mandatory,
deterministic L0 checks per cited evidence id:

  exists · in active version · ACL-visible to requester · in requester's
  retrieval ledger · not stale · supporting trust is EXTRACTED.

A claim passes L0 iff every cited evidence passes every check. ``overall`` is
``passed`` iff all claims passed, ``failed`` iff all failed, else ``partial``.

The verifier performs NO generation and treats answer/evidence text as
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
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.error_ledger import write_error_event
from agentic_mcp_server.context_broker.trust import CLAIM_SUPPORTING
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_kb_version
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
    ClaimReceipt,
    L0Checks,
    VerificationReceipt,
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


def _check_evidence(raw_id: str, ctx: _EvidenceContext) -> tuple[L0Checks, list[str]]:
    """Run the six L0 checks for one cited evidence id; return checks + reasons."""
    reasons: list[str] = []
    try:
        artifact_id = uuid.UUID(raw_id)
    except ValueError:
        # A malformed id cannot reference any artifact: every check fails.
        return (
            L0Checks(
                L0_exists=False,
                L0_in_active_version=False,
                L0_acl_visible=False,
                L0_in_requester_ledger=False,
                L0_not_stale=False,
                L0_supporting_trust_ok=False,
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

    checks = L0Checks(
        L0_exists=exists,
        L0_in_active_version=in_active_version,
        L0_acl_visible=acl_visible,
        L0_in_requester_ledger=in_ledger,
        L0_not_stale=not_stale,
        L0_supporting_trust_ok=supporting_trust_ok,
    )
    return checks, reasons


def _merge_checks(into: L0Checks, other: L0Checks) -> L0Checks:
    """A claim's per-claim checks are the AND of every cited evidence's checks."""
    return L0Checks(
        L0_exists=into.L0_exists and other.L0_exists,
        L0_in_active_version=into.L0_in_active_version and other.L0_in_active_version,
        L0_acl_visible=into.L0_acl_visible and other.L0_acl_visible,
        L0_in_requester_ledger=into.L0_in_requester_ledger and other.L0_in_requester_ledger,
        L0_not_stale=into.L0_not_stale and other.L0_not_stale,
        L0_supporting_trust_ok=into.L0_supporting_trust_ok and other.L0_supporting_trust_ok,
    )


async def verify_answer(
    deps: BrokerDeps, request: VerifyAnswerRequest, requester: Requester
) -> VerificationReceipt:
    started = time.monotonic()
    answer_hash = _normalized_answer_hash(request)

    async with deps.session_factory() as session:
        active_version = await fetch_active_kb_version(session)
        if active_version is None:
            await write_error_event(
                deps,
                tool_name=_TOOL_NAME,
                subject=requester.subject,
                query_text=request.answer_id,
            )
            raise ToolError("no active kb_version; the knowledge base has not been built yet")

        # null graph_version ⇒ active; a pinned version must equal the served
        # one (we serve exactly the last successful active version, invariant 5).
        graph_version = request.graph_version or active_version

        # Resolve every cited id once, then run pure per-claim checks over it.
        cited_ids: list[uuid.UUID] = []
        for claim in request.claims:
            for raw in claim.evidence_ids:
                try:
                    cited_ids.append(uuid.UUID(raw))
                except ValueError:
                    continue
        unique_ids = list(dict.fromkeys(cited_ids))

        if graph_version == active_version:
            in_version = await fetch_provenance(
                session, unique_ids, graph_version, extracted_bucket=CLAIM_SUPPORTING
            )
        else:
            # A pinned non-active version is, by construction, not the served
            # one: nothing belongs to the active version under L0's contract.
            in_version = {}
        exists_anywhere = await fetch_existing_anywhere(session, unique_ids)
        retrieved = await fetch_subject_retrieved_ids(session, requester.subject)

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
        merged: L0Checks | None = None
        reasons: list[str] = []
        for raw_id in claim.evidence_ids:
            checks, evidence_reasons = _check_evidence(raw_id, ctx)
            merged = checks if merged is None else _merge_checks(merged, checks)
            reasons.extend(evidence_reasons)
        # merged is never None: the schema rejects a claim with empty evidence.
        assert merged is not None
        passed = (
            merged.L0_exists
            and merged.L0_in_active_version
            and merged.L0_acl_visible
            and merged.L0_in_requester_ledger
            and merged.L0_not_stale
            and merged.L0_supporting_trust_ok
        )
        claim_results.append(
            ClaimReceipt(
                claim_id=claim.claim_id,
                result="passed" if passed else "failed",
                checks=merged,
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
        "overall=%s passed=%d",
        request.answer_id,
        requester.subject,
        graph_version,
        len(claim_results),
        overall,
        passed_count,
    )

    return VerificationReceipt(
        answer_hash=answer_hash,
        graph_version=graph_version,
        issued_at=datetime.now(UTC),
        verifier_levels_run=["L0"],
        overall=overall,
        claim_results=claim_results,
        client_id=None,
        signature=None,
    )
