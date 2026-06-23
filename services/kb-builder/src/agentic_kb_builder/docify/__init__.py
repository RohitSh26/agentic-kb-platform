"""Docify pipeline (ADR-0023): Graphify LLM doc extraction -> doc artifacts (artifacts-only).

The prose extraction pipeline. Document sources (github_doc / azure_wiki /
ado_card) route through Graphify's LLM doc extractor; the trust-sensitive normalization is
the pure ``map_doc_extraction`` mapper. Produces ARTIFACTS ONLY — no edges (parity with the
the relation ontology). Cache-gated by the build runner (a hit makes no LLM
call); the Graphify extraction function is injectable for hermetic tests.
"""

from agentic_kb_builder.docify.docify_backend import map_doc_extraction
from agentic_kb_builder.docify.extract_fn import (
    DocExtractFn,
    make_graphify_doc_extract,
    resolve_endpoint,
)
from agentic_kb_builder.docify.extractor import DocExtractor
from agentic_kb_builder.docify.write import write_doc_artifacts

__all__ = [
    "DocExtractFn",
    "DocExtractor",
    "make_graphify_doc_extract",
    "map_doc_extraction",
    "resolve_endpoint",
    "write_doc_artifacts",
]
