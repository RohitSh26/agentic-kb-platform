"""Deterministic, precision-biased link matching (architecture §5, §14).

Exact textual evidence only: a symbol's qualified name, a file path, an
endpoint title, or a concept title appearing verbatim in another artifact's
text. Anything weaker is left to the semantic pass — over-linking is the
brief's explicit failure mode, so short or single-word titles that would match
incidentally are excluded entirely. Patterns are compiled once per title (not
per document pair) and guarded by substring prefilters, keeping the nightly
docs x titles scan cheap.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agentic_kb_builder.domain import LinkEdgeDraft, LinkerEdgeType
from agentic_kb_builder.linker.cross_domain import (
    find_cross_domain_links,
    find_doc_work_item_mentions,
)
from agentic_kb_builder.linker.records import (
    CARD_SOURCE_TYPES,
    CODE_ARTIFACT_TYPES,
    DOC_ARTIFACT_TYPES,
    DOC_SOURCE_TYPES,
    LinkableArtifact,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

IMPLEMENTS_CONFIDENCE = 0.95
DOC_LINK_CONFIDENCE = 0.9
# Precision guards: a concept title participates only if it is multi-word or
# reasonably long; symbol names must be long enough not to match incidentally.
MIN_CONCEPT_TITLE_CHARS = 6
MIN_SYMBOL_TITLE_CHARS = 4


@dataclass(frozen=True)
class _Matcher:
    artifact: LinkableArtifact
    needle: str
    pattern: re.Pattern[str]
    casefold_prefilter: bool


def find_deterministic_links(artifacts: Sequence[LinkableArtifact]) -> list[LinkEdgeDraft]:
    concept_matchers = [
        _Matcher(a, a.title.lower(), _phrase_pattern(a.title), casefold_prefilter=True)
        for a in artifacts
        if a.artifact_type == "concept" and a.title is not None and _eligible_concept_title(a.title)
    ]
    symbol_matchers = [
        _Matcher(a, a.title, _symbol_pattern(a.title), casefold_prefilter=False)
        for a in artifacts
        if a.artifact_type == "code_symbol"
        and a.title is not None
        and len(a.title) >= MIN_SYMBOL_TITLE_CHARS
    ]
    code_matchers = symbol_matchers + [
        # code_file titles are paths, endpoint titles are "METHOD /route"; both
        # get boundary guards so "src/a/b.py" never matches inside "other/src/a/b.py".
        _Matcher(a, a.title, _path_pattern(a.title), casefold_prefilter=False)
        for a in artifacts
        if a.artifact_type in CODE_ARTIFACT_TYPES
        and a.artifact_type != "code_symbol"
        and a.title is not None
    ]
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

    for concept_matcher in concept_matchers:
        concept = concept_matcher.artifact
        concept_text = "\n".join(part for part in (concept.title, concept.body_text) if part)
        concept_text_lower = concept_text.lower()
        for symbol_matcher in symbol_matchers:
            if _matches(symbol_matcher, concept_text, concept_text_lower):
                add(symbol_matcher.artifact, concept, "implements", IMPLEMENTS_CONFIDENCE)

    for doc in docs:
        body = doc.body_text or ""
        body_lower = body.lower()
        edge_type: LinkerEdgeType = (
            "requests" if doc.source_type in CARD_SOURCE_TYPES else "documents"
        )
        for concept_matcher in concept_matchers:
            if _matches(concept_matcher, body, body_lower):
                add(doc, concept_matcher.artifact, edge_type, DOC_LINK_CONFIDENCE)
        for code_matcher in code_matchers:
            if _matches(code_matcher, body, body_lower):
                add(doc, code_matcher.artifact, "mentions", DOC_LINK_CONFIDENCE)

    # Cross-domain deterministic rules: commit→work-item implements,
    # commit→code_file mentions, and doc→work-item mentions. They carry an
    # evidence pointer; merge them through the same (from,to,edge_type) dedupe so
    # a logical link is never emitted twice.
    cross_domain = find_cross_domain_links(artifacts)
    cross_domain += find_doc_work_item_mentions(docs, artifacts)
    for draft in cross_domain:
        key = (draft.from_artifact_id, draft.to_artifact_id, str(draft.edge_type))
        if key in seen:
            continue
        seen.add(key)
        drafts.append(draft)

    logger.info(
        "event=linker_deterministic_matched concepts=%d symbols=%d docs=%d edges=%d",
        len(concept_matchers),
        len(symbol_matchers),
        len(docs),
        len(drafts),
    )
    return drafts


def _matches(matcher: _Matcher, text: str, text_lower: str) -> bool:
    # Substring prefilter: the regex only runs when the raw needle occurs at all.
    haystack = text_lower if matcher.casefold_prefilter else text
    if matcher.needle not in haystack:
        return False
    return matcher.pattern.search(text) is not None


def _eligible_concept_title(title: str) -> bool:
    return " " in title.strip() or len(title) >= MIN_CONCEPT_TITLE_CHARS


def _symbol_pattern(qualified_name: str) -> re.Pattern[str]:
    # Boundaries exclude identifier chars and dots so "get_user" never matches
    # inside "get_user_embedding" or "Service.get_user_embedding".
    return re.compile(rf"(?<![\w.]){re.escape(qualified_name)}(?![\w])")


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)


def _path_pattern(title: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w/]){re.escape(title)}(?![\w/])")
