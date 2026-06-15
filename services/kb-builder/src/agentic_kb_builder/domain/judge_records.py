"""Relationship-judge shapes: the LLM verdict over ONE bounded candidate pair.

Phase 3B of ADR-0010 / ADR-0011. The judge looks ONLY at candidates the cheap
generator surfaced (relationship-candidates.md) and rules on whether the pair is a
real relationship, under the closed relation ontology (relation-ontology.md) and
trust buckets (trust-buckets.md).

Hard invariants encoded here (enforced again at the call boundary):

- ``relation_type`` is a closed ontology vocabulary. The judge may NEVER emit
  ``related_to`` (banned catch-all) or ``EXTRACTED``-only deterministic relations
  the AST owns — only relations a prose/cross-domain judge is allowed to infer.
- ``trust_bucket`` is one of the LLM-judge buckets; the judge may NEVER assign
  ``EXTRACTED`` (that bucket is reserved for deterministic producers).
- ``supporting_quote`` must be a verbatim substring of one of the cited source
  spans (quote-guard, invariant 7); the caller downgrades to ``AMBIGUOUS`` when it
  is not, so a fabricated quote can never become an INFERRED edge.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Relations the LLM judge is allowed to INFER over a cross-domain candidate pair.
# A strict subset of the relation ontology: the AST-only deterministic relations
# (imports/calls/inherits/exposes/tests/implements) and the verbatim-match
# `mentions` are NOT judge-produced — only prose->code `documents`. `related_to`
# is banned everywhere (relation-ontology.md).
JudgeRelationType = Literal["documents"]
JUDGE_RELATION_TYPES: frozenset[str] = frozenset({"documents"})

# Buckets the LLM judge may assign (trust-buckets.md). EXTRACTED is reserved for
# deterministic producers and is intentionally absent.
JudgeTrustBucket = Literal["INFERRED_HIGH", "INFERRED_LOW", "AMBIGUOUS", "REJECTED"]
JUDGE_TRUST_BUCKETS: frozenset[str] = frozenset(
    {"INFERRED_HIGH", "INFERRED_LOW", "AMBIGUOUS", "REJECTED"}
)
# The buckets that become a knowledge_edge the broker may route on (as a hint).
INFERRED_EDGE_BUCKETS: frozenset[str] = frozenset({"INFERRED_HIGH", "INFERRED_LOW"})


@dataclass(frozen=True)
class JudgeEndpoint:
    """One side of a candidate pair, as the judge sees it: identity + the evidence
    span the quote-guard checks the supporting_quote against. ``evidence_text`` is
    the verbatim source span (artifact body); ``content_hash`` keys the cache."""

    artifact_id: uuid.UUID
    title: str
    evidence_text: str
    content_hash: str


@dataclass(frozen=True)
class JudgeCandidate:
    """The bounded input the judge rules on: the two artifacts' titles + evidence
    spans. ``from_endpoint`` -> ``to_endpoint`` is the candidate's ordered pair
    (the generator's direction). The judge NEVER sees anything beyond these spans
    (no global context — only the retrieved candidate)."""

    from_endpoint: JudgeEndpoint
    to_endpoint: JudgeEndpoint

    @property
    def cited_spans(self) -> tuple[str, str]:
        """The source spans the supporting_quote MUST be a verbatim substring of."""
        return (self.from_endpoint.evidence_text, self.to_endpoint.evidence_text)


class RelationshipJudgment(BaseModel):
    """The judge's ruling on ONE candidate pair.

    ``supporting_quote`` is the verbatim span the verdict rests on; the caller
    quote-guards it against the cited source spans before any edge is written.
    """

    model_config = ConfigDict(frozen=True)

    relation_type: JudgeRelationType
    trust_bucket: JudgeTrustBucket
    supporting_quote: str
    reason: str


def quote_is_grounded(quote: str, *, cited_spans: tuple[str, ...]) -> bool:
    """Quote-guard (invariant 7): the supporting_quote must be a verbatim substring
    of one of the cited source spans. Whitespace is collapsed on both sides so a
    model that reflows internal spacing is not rejected, but the quote is otherwise
    an exact, fabrication-proof match — never a fuzzy/semantic one. An empty quote
    is never grounded."""
    needle = " ".join(quote.split())
    if not needle:
        return False
    return any(needle in " ".join(span.split()) for span in cited_spans)


def guard_quote(
    judgment: "RelationshipJudgment", *, cited_spans: tuple[str, ...]
) -> "RelationshipJudgment":
    """Return the judgment unchanged when its quote is grounded; otherwise DOWNGRADE
    it to AMBIGUOUS (kept out of default traversal). A non-verbatim quote means the
    verdict's evidence cannot be trusted, so it must never become an INFERRED edge."""
    if judgment.trust_bucket in INFERRED_EDGE_BUCKETS and not quote_is_grounded(
        judgment.supporting_quote, cited_spans=cited_spans
    ):
        return judgment.model_copy(update={"trust_bucket": "AMBIGUOUS"})
    return judgment


__all__ = [
    "INFERRED_EDGE_BUCKETS",
    "JUDGE_RELATION_TYPES",
    "JUDGE_TRUST_BUCKETS",
    "JudgeCandidate",
    "JudgeEndpoint",
    "JudgeRelationType",
    "JudgeTrustBucket",
    "RelationshipJudgment",
    "guard_quote",
    "quote_is_grounded",
]
