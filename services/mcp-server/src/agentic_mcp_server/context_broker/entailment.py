"""L3 verifier: cached LLM entailment for deterministically-unresolved claims (PR-31).

This is the ONLY non-deterministic verifier level, so it is gated hard by cost
discipline (ADR-0011, verification-receipt.md):

  - It runs ONLY for a claim L0-L2 could not adjudicate — i.e. the claim passed
    every deterministic level that ran but carries no typed assertion L2 settled.
    It NEVER runs on a claim L2 already resolved (pass or fail) and never on a
    claim already failing a deterministic level.
  - It requires ≥1 RESOLVABLE cited unit (real, in-version, ACL-visible,
    requester-retrieved — the same set L1 uses). A claim with no resolvable
    evidence has nothing to entail against and is skipped.

Every entailment is GATED by ``entailment_cache`` (architecture invariant 4): a hit
returns the stored verdict with ZERO LLM calls. The cache key is
``(claim_hash, evidence_ids_hash, prompt_version, model_version)`` — claim text hash,
a stable hash over the SORTED resolvable cited evidence ids, the prompt version, and
the model version. The verifier reads only the claim's OWN resolvable cited evidence
texts (never raw text the requester did not retrieve — invariant 6), and never logs
answer or evidence text.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agentic_mcp_server.infrastructure.entailment.client import EntailmentClient
from agentic_mcp_server.infrastructure.entailment.ollama_client import ENTAILMENT_PROMPT_VERSION
from agentic_mcp_server.infrastructure.postgres.artifacts import fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.entailment_cache import (
    lookup_entailment,
    record_entailment,
)

logger = logging.getLogger(__name__)

#: failed_reason emitted when L3 ran and the evidence did NOT entail the claim.
REASON_ENTAILMENT_UNSUPPORTED = "entailment_unsupported"


@dataclass(frozen=True)
class EntailmentOutcome:
    """One claim's L3 verdict: ``entailed`` (None ⇒ L3 did not run for this claim)."""

    entailed: bool | None
    cache_hit: bool


def _claim_hash(claim_text: str) -> str:
    """Stable hash of the whitespace-normalized claim text (cache key part)."""
    normalized = " ".join(claim_text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _evidence_ids_hash(evidence_ids: frozenset[uuid.UUID]) -> str:
    """Stable hash over the SORTED resolvable cited evidence ids (cache key part).

    Sorting makes the key order-independent: the same set of resolvable cited units
    resolves to one cache row. A change to the cited, resolvable set re-keys ⇒ a
    miss ⇒ a fresh entailment.
    """
    canonical = ",".join(sorted(str(uid) for uid in evidence_ids))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def run_l3_entailment(
    session: AsyncSession,
    *,
    client: EntailmentClient,
    claim_text: str,
    resolvable_cited_ids: frozenset[uuid.UUID],
    build_seq: int,
) -> EntailmentOutcome:
    """Cached entailment verdict for ONE deterministically-unresolved claim.

    The caller has already established this claim is L3-eligible (passed every
    deterministic level, carries no L2 assertion, has resolvable evidence). Returns
    the verdict + whether it came from cache. A cache hit makes ZERO LLM calls.
    """
    claim_hash = _claim_hash(claim_text)
    evidence_ids_hash = _evidence_ids_hash(resolvable_cited_ids)
    model_version = client.model_version

    cached = await lookup_entailment(
        session,
        claim_hash=claim_hash,
        evidence_ids_hash=evidence_ids_hash,
        prompt_version=ENTAILMENT_PROMPT_VERSION,
        model_version=model_version,
    )
    if cached is not None:
        entailed, _reason = cached
        # Cache hit: ZERO LLM calls (architecture invariant 4).
        return EntailmentOutcome(entailed=entailed, cache_hit=True)

    # Miss ⇒ resolve the claim's OWN resolvable cited evidence texts and call the
    # model. fetch_artifacts is membership-filtered; we keep only the resolvable
    # cited ids (already ACL-checked by the caller) so no unretrieved text leaks.
    rows = await fetch_artifacts(session, list(resolvable_cited_ids), build_seq)
    evidence_texts = [
        row.body_text for row in rows if row.artifact_id in resolvable_cited_ids and row.body_text
    ]
    verdict = await client.check_entailment(claim_text=claim_text, evidence_texts=evidence_texts)

    # Idempotent write so the next verify of this claim skips the LLM.
    await record_entailment(
        session,
        claim_hash=claim_hash,
        evidence_ids_hash=evidence_ids_hash,
        prompt_version=ENTAILMENT_PROMPT_VERSION,
        model_version=model_version,
        entailed=verdict.entailed,
        reason=verdict.reason,
    )
    return EntailmentOutcome(entailed=verdict.entailed, cache_hit=False)


__all__ = ["REASON_ENTAILMENT_UNSUPPORTED", "EntailmentOutcome", "run_l3_entailment"]
