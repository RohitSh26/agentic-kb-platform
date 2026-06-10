"""Deterministic, precision-biased link matching (architecture §5, §14).

Exact textual evidence only: a symbol's qualified name, a file path, an
endpoint title, or a concept title appearing verbatim in another artifact's
text. Anything weaker is left to the semantic pass — over-linking is the
brief's explicit failure mode, so short or single-word titles that would match
incidentally are excluded entirely.
"""

import re
from collections.abc import Sequence

from common.logging import get_logger
from contracts.artifact_schemas import LinkEdgeDraft, LinkerEdgeType
from kb_builder.linker.records import (
    CARD_SOURCE_TYPES,
    CODE_ARTIFACT_TYPES,
    DOC_ARTIFACT_TYPES,
    DOC_SOURCE_TYPES,
    LinkableArtifact,
)

logger = get_logger("kb_builder.linker.deterministic")

IMPLEMENTS_CONFIDENCE = 0.95
DOC_LINK_CONFIDENCE = 0.9
# Precision guards: a concept title participates only if it is multi-word or
# reasonably long; symbol names must be long enough not to match incidentally.
MIN_CONCEPT_TITLE_CHARS = 6
MIN_SYMBOL_TITLE_CHARS = 4


def find_deterministic_links(artifacts: Sequence[LinkableArtifact]) -> list[LinkEdgeDraft]:
    concepts = [a for a in artifacts if a.artifact_type == "concept" and _eligible_concept(a)]
    symbols = [
        a
        for a in artifacts
        if a.artifact_type == "code_symbol"
        and a.title is not None
        and len(a.title) >= MIN_SYMBOL_TITLE_CHARS
    ]
    code = [a for a in artifacts if a.artifact_type in CODE_ARTIFACT_TYPES]
    docs = [
        a
        for a in artifacts
        if a.artifact_type in DOC_ARTIFACT_TYPES
        and a.source_type in (DOC_SOURCE_TYPES | CARD_SOURCE_TYPES)
        and a.body_text is not None
    ]

    drafts: list[LinkEdgeDraft] = []
    seen: set[tuple[object, object, str]] = set()

    def add(
        from_a: LinkableArtifact, to_a: LinkableArtifact, edge_type: LinkerEdgeType, conf: float
    ) -> None:
        key = (from_a.artifact_id, to_a.artifact_id, edge_type)
        if key in seen or from_a.artifact_id == to_a.artifact_id:
            return
        seen.add(key)
        drafts.append(
            LinkEdgeDraft(
                from_artifact_id=from_a.artifact_id,
                to_artifact_id=to_a.artifact_id,
                edge_type=edge_type,
                confidence=conf,
                strategy="deterministic",
            )
        )

    for concept in concepts:
        concept_text = _text_of(concept)
        for symbol in symbols:
            assert symbol.title is not None
            if _contains_symbol(concept_text, symbol.title):
                add(symbol, concept, "implements", IMPLEMENTS_CONFIDENCE)

    for doc in docs:
        assert doc.body_text is not None
        edge_type: LinkerEdgeType = (
            "requests" if doc.source_type in CARD_SOURCE_TYPES else "documents"
        )
        for concept in concepts:
            assert concept.title is not None
            if _contains_phrase(doc.body_text, concept.title):
                add(doc, concept, edge_type, DOC_LINK_CONFIDENCE)
        for target in code:
            if target.title is None:
                continue
            if target.artifact_type == "code_symbol":
                matched = len(target.title) >= MIN_SYMBOL_TITLE_CHARS and _contains_symbol(
                    doc.body_text, target.title
                )
            else:
                # code_file titles are paths, endpoint titles are "METHOD /route";
                # both are distinctive enough for verbatim containment.
                matched = target.title in doc.body_text
            if matched:
                add(doc, target, "mentions", DOC_LINK_CONFIDENCE)

    logger.info(
        "event=linker_deterministic_matched concepts=%d symbols=%d docs=%d edges=%d",
        len(concepts),
        len(symbols),
        len(docs),
        len(drafts),
    )
    return drafts


def _eligible_concept(artifact: LinkableArtifact) -> bool:
    title = artifact.title
    if title is None:
        return False
    return " " in title.strip() or len(title) >= MIN_CONCEPT_TITLE_CHARS


def _text_of(artifact: LinkableArtifact) -> str:
    return "\n".join(part for part in (artifact.title, artifact.body_text) if part)


def _contains_symbol(text: str, qualified_name: str) -> bool:
    # Boundaries exclude identifier chars and dots so "get_user" never matches
    # inside "get_user_embedding" or "Service.get_user_embedding".
    pattern = rf"(?<![\w.]){re.escape(qualified_name)}(?![\w])"
    return re.search(pattern, text) is not None


def _contains_phrase(text: str, phrase: str) -> bool:
    pattern = rf"\b{re.escape(phrase)}\b"
    return re.search(pattern, text, re.IGNORECASE) is not None
