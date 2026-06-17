"""Pure-mapper unit tests for docify (ADR-0023): the trust-derivation crown jewel.

Hermetic — no DB, no live LLM. ``map_doc_extraction`` is exercised against captured-shape
Graphify doc-extraction fixtures to prove the artifact trust axis (ADR-0023 §3):

- Axis A (artifact knowledge_kind): a concept whose source_location IS a verbatim source
  substring -> source_backed (carrying the quote); one that is NOT -> interpreted; the
  document node -> interpreted summary; whitespace-only / paraphrased never grounds.

Docify produces ARTIFACTS ONLY — concept->concept relations are NOT materialized as edges
(relation-ontology); the mapper never emits edges.
"""

from typing import Any

from agentic_kb_builder.docify.docify_backend import (
    CONCEPT_AUTHORITY,
    FACT_AUTHORITY,
    SUMMARY_AUTHORITY,
    map_doc_extraction,
)
from agentic_kb_builder.domain import DocArtifactDraft

DOC_PATH = "docs/auth.md"
SOURCE_TEXT = (
    "# Auth guide\n\n"
    "The login flow validates a session token against the AuthMiddleware.\n"
    "Tokens are refreshed by the rotation job every hour.\n"
)


def _node(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "file_type": "concept",
        "source_file": DOC_PATH,
        "source_url": None,
        "captured_at": None,
        "author": None,
        "contributor": None,
    }
    base.update(kw)
    return base


def _captured() -> dict[str, Any]:
    """A captured-shape Graphify doc extraction for SOURCE_TEXT."""
    return {
        "nodes": [
            # document node: source_location is a HEADING, never a quote.
            {
                "id": "doc:auth",
                "label": "Auth guide",
                "file_type": "document",
                "source_file": DOC_PATH,
                "source_location": "# Auth guide",
            },
            # grounded concept: source_location IS a verbatim source sentence.
            _node(
                id="login_flow",
                label="login flow",
                source_location=(
                    "The login flow validates a session token against the AuthMiddleware."
                ),
            ),
            # paraphrased concept: source_location is NOT in the source verbatim.
            _node(
                id="token_rotation",
                label="token rotation",
                source_location="The system periodically rotates authentication credentials.",
            ),
            # an EXTERNAL node (different file) must be dropped — not our doc.
            _node(
                id="other",
                label="other",
                source_file="docs/other.md",
                source_location="unrelated",
            ),
        ],
        "edges": [
            {
                "source": "login_flow",
                "target": "token_rotation",
                "relation": "conceptually_related_to",
                "confidence": "EXTRACTED",
            }
        ],
        "input_tokens": 100,
        "output_tokens": 50,
        "model": "groq:llama",
        "finish_reason": "stop",
    }


def test_grounded_concept_becomes_source_backed_with_the_verbatim_quote() -> None:
    result = map_doc_extraction(_captured(), source_text=SOURCE_TEXT, known_doc_path=DOC_PATH)
    backed = [a for a in result.artifacts if a.artifact_type == "source_backed_fact"]
    assert len(backed) == 1
    fact = backed[0]
    assert fact.knowledge_kind == "source_backed"
    assert fact.title == "login flow"
    # the body IS the verbatim supporting sentence (what the L0 verifier confirms).
    assert (
        fact.body_text
        == "The login flow validates a session token against the AuthMiddleware."
    )
    assert fact.body_text in SOURCE_TEXT
    assert fact.authority_score == FACT_AUTHORITY


def test_paraphrased_concept_becomes_interpreted_never_a_false_citation() -> None:
    result = map_doc_extraction(_captured(), source_text=SOURCE_TEXT, known_doc_path=DOC_PATH)
    concepts = [a for a in result.artifacts if a.artifact_type == "concept"]
    assert len(concepts) == 1
    concept = concepts[0]
    assert concept.knowledge_kind == "interpreted"
    assert concept.title == "token rotation"
    # the paraphrase is NEVER stored as a verbatim quote.
    assert concept.authority_score == CONCEPT_AUTHORITY
    assert "periodically rotates" not in SOURCE_TEXT
    # interpreted sits strictly below source-backed.
    assert CONCEPT_AUTHORITY < FACT_AUTHORITY


def test_document_node_becomes_interpreted_summary() -> None:
    result = map_doc_extraction(_captured(), source_text=SOURCE_TEXT, known_doc_path=DOC_PATH)
    summaries = [a for a in result.artifacts if a.artifact_type == "summary"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.knowledge_kind == "interpreted"
    assert summary.authority_score == SUMMARY_AUTHORITY


def test_external_file_node_is_dropped() -> None:
    result = map_doc_extraction(_captured(), source_text=SOURCE_TEXT, known_doc_path=DOC_PATH)
    titles = {a.title for a in result.artifacts}
    assert "other" not in titles


def test_docify_produces_no_edges() -> None:
    # The raw Graphify extraction DOES carry concept->concept relations...
    data = _captured()
    assert data["edges"], "fixture must carry raw concept relations for this test to be meaningful"
    # ...but the mapper DROPS them (relation-ontology bans generic concept-relatedness as an edge;
    # parity with the wikify it replaces, which wrote none): the result exposes artifacts only.
    result = map_doc_extraction(data, source_text=SOURCE_TEXT, known_doc_path=DOC_PATH)
    assert not hasattr(result, "edges")
    assert result.artifacts and all(isinstance(a, DocArtifactDraft) for a in result.artifacts)


def test_whitespace_only_source_location_never_grounds() -> None:
    data = _captured()
    # a whitespace-only supporting sentence normalizes to empty -> never grounds (axis A).
    data["nodes"][1]["source_location"] = "   \n\t  "
    result = map_doc_extraction(data, source_text=SOURCE_TEXT, known_doc_path=DOC_PATH)
    # login_flow now falls to interpreted; only token_rotation was already interpreted.
    backed = [a for a in result.artifacts if a.artifact_type == "source_backed_fact"]
    assert backed == []
    interpreted = [a for a in result.artifacts if a.artifact_type == "concept"]
    assert {c.title for c in interpreted} == {"login flow", "token rotation"}


def test_grounding_tolerates_reflowed_whitespace_like_the_verifier() -> None:
    # The shared whitespace normalization: a source_location that differs ONLY in
    # incidental whitespace still grounds (matches verify._normalize_whitespace).
    data = _captured()
    data["nodes"][1]["source_location"] = (
        "The login flow   validates a session\ntoken against the AuthMiddleware."
    )
    result = map_doc_extraction(data, source_text=SOURCE_TEXT, known_doc_path=DOC_PATH)
    backed = [a for a in result.artifacts if a.artifact_type == "source_backed_fact"]
    assert len(backed) == 1
    assert backed[0].title == "login flow"
