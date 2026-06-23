"""Pure mapper: Graphify LLM doc extraction -> our doc artifacts.

Mirrors graphify.graphify_backend.map_extraction: a pure, deterministic, I/O-free function
that re-normalizes Graphify's raw doc output into our trust contract, hermetically testable
against a captured fixture. Docify produces ARTIFACTS ONLY — no edges (parity with the
the relation ontology, which wrote none). Graphify's concept->concept relations are generic
relatedness, which the relation ontology bans as an edge
(docs/contracts/relation-ontology.md: "no generic related_to ... it becomes a candidate ...
never an edge"); ``mentions`` is contractually reserved for verbatim-identifier EXTRACTED
matches, not LLM concept relations. Promoting these relations to phase-3 candidates is a
tracked follow-up.

The mapper re-derives the artifact trust axis (axis A) — it never copies Graphify's labels:

- Axis A (artifact knowledge_kind, per concept node): a concept whose ``source_location``
  is a verbatim substring of the source text -> a ``source_backed_fact`` carrying the quote;
  else an ``interpreted`` ``concept``. The substring check uses the SAME whitespace
  normalization as the broker's L0 verifier (`verify._normalize_whitespace`), duplicated here
  because services may not import each other. The document node -> an ``interpreted`` summary.
"""

from collections.abc import Mapping
from typing import Any, cast

from agentic_kb_builder.domain import (
    DocArtifactDraft,
    DocExtractionResult,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# V1 seed scores (stable seed scores so the registry row shape is
# frozen,. Interpreted knowledge (summary/concept) sits strictly below the
# source-backed quote so the broker can never rank a paraphrase as final truth.
FACT_AUTHORITY = 0.8
CONCEPT_AUTHORITY = 0.6
SUMMARY_AUTHORITY = 0.5
# Freshly extracted from the current source version; runtime decay is broker policy.
BUILD_TIME_FRESHNESS = 1.0


def _normalize_whitespace(value: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    DUPLICATED from the broker's L0 verifier (`verify._normalize_whitespace`,
    services/mcp-server) — services may not import each other, so the rule is duplicated,
    NOT diverged. The two layers MUST share one normalization so an artifact promoted to
    ``source_backed`` at build time cannot fail L0 grounding at read time.
    """
    return " ".join(value.split())


def _is_grounded(source_location: object, *, normalized_source: str) -> bool:
    """True iff the concept's source_location is a verbatim substring of the source text.

    Uses the shared whitespace normalization. An empty normalized needle NEVER grounds
    (a fabricated/blank quote must not pass), matching the verifier's guard exactly."""
    if not isinstance(source_location, str):
        return False
    needle = _normalize_whitespace(source_location)
    if not needle:
        return False
    return needle in normalized_source


def map_doc_extraction(
    data: Mapping[str, Any],
    *,
    source_text: str,
    known_doc_path: str,
) -> DocExtractionResult:
    """Normalize a Graphify doc-extraction dict into our doc artifacts (artifacts-only).

    Pure and deterministic (no I/O) so it is hermetically testable against a captured
    fixture. ``source_text`` is the verbatim normalized source the document was extracted
    from (the grounding corpus for axis A). ``known_doc_path`` is the repo-relative doc
    path; nodes whose ``source_file`` is some OTHER file are external references and are
    dropped (never an artifact for a file we did not extract). Concept->concept relations
    are NOT materialized as edges.
    """
    nodes = cast("list[Mapping[str, Any]]", list(data.get("nodes", [])))

    normalized_source = _normalize_whitespace(source_text)

    def in_doc(node: Mapping[str, Any]) -> bool:
        # Graphify reports source_file repo-relative under its root; a node for any other
        # file is an external reference and is not ours to materialize.
        return str(node.get("source_file", "")) == known_doc_path

    artifacts: list[DocArtifactDraft] = []
    grounded = 0
    interpreted = 0
    for node in nodes:
        if not in_doc(node):
            continue
        node_id = str(node.get("id", ""))
        file_type = str(node.get("file_type", ""))
        label = node.get("label")
        if file_type == "document":
            # The document node's source_location is a HEADING, not a verbatim quote:
            # always an interpreted summary/pointer (axis A).
            artifacts.append(
                DocArtifactDraft(
                    artifact_type="summary",
                    knowledge_kind="interpreted",
                    title=str(label) if label is not None else None,
                    body_text=str(label) if label else known_doc_path,
                    authority_score=SUMMARY_AUTHORITY,
                    freshness_score=BUILD_TIME_FRESHNESS,
                )
            )
            continue
        if file_type != "concept":
            continue  # any other node type is out of the doc ontology; no artifact
        if not node_id:
            continue
        source_location = node.get("source_location")
        concept_name = str(label) if label is not None else None
        if _is_grounded(source_location, normalized_source=normalized_source):
            # Axis A: verbatim-anchored -> a source_backed_fact whose body carries the
            # actual supporting sentence (what the L0 verifier / verify_answer confirm).
            grounded += 1
            artifacts.append(
                DocArtifactDraft(
                    artifact_type="source_backed_fact",
                    knowledge_kind="source_backed",
                    title=concept_name,
                    body_text=str(source_location),
                    authority_score=FACT_AUTHORITY,
                    freshness_score=BUILD_TIME_FRESHNESS,
                )
            )
        else:
            # Axis A: no verbatim anchor -> interpreted concept (the model paraphrased);
            # ranked below source-backed evidence, never a false citation (invariant 7).
            interpreted += 1
            body = concept_name or node_id
            artifacts.append(
                DocArtifactDraft(
                    artifact_type="concept",
                    knowledge_kind="interpreted",
                    title=concept_name,
                    body_text=body,
                    authority_score=CONCEPT_AUTHORITY,
                    freshness_score=BUILD_TIME_FRESHNESS,
                )
            )

    logger.info(
        "event=docify_mapped doc_path=%s source_backed=%d interpreted=%d",
        known_doc_path,
        grounded,
        interpreted,
    )
    return DocExtractionResult(artifacts=tuple(artifacts))


__all__ = ["map_doc_extraction"]
