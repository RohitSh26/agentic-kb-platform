"""Canonical code artifacts/edges produced from Graphify's whole-tree extraction.

Graphify (the library) owns code-structure extraction (ADR-0012); these are the shapes
our mapping layer (graphify.graphify_backend) emits into the versioned, ACL'd Postgres
registry. Symbols carry exact spans (recovered deterministically — Graphify reports only
a start line) so L2 evidence can return precise snippets at a source version. Edge drafts
use symbolic keys (resolved to artifact uuids only after persistence): "file:{path}",
"sym:{path}::{name}", "test:{path}::{name}", "endpoint:{path}::{method} {route}".
"""

from typing import Literal

from pydantic import Field

from agentic_kb_builder.domain.artifact_model import ArtifactModel

CodeArtifactType = Literal["code_file", "code_symbol", "endpoint", "test"]
# imports/calls/tests/exposed_as/defined_in are the original structural set; uses/references/
# inherits are Graphify's richer cross-file symbol relations (a symbol uses/type-references/
# subclasses another), ingested so the broker can resolve symbol-level dependencies.
CodeEdgeType = Literal[
    "imports", "calls", "tests", "exposed_as", "defined_in", "uses", "references", "inherits"
]
SymbolKind = Literal["function", "class", "method"]


class CodeArtifactDraft(ArtifactModel):
    """One code knowledge_artifact row; always source_backed knowledge."""

    key: str = Field(min_length=1)
    artifact_type: CodeArtifactType
    title: str = Field(min_length=1)
    body_text: str | None = Field(
        default=None,
        description="exact snippet for symbols/tests; None for code_file (pointer only)",
    )
    span_start: int | None = Field(default=None, ge=1)
    span_end: int | None = Field(default=None, ge=1)
    search_text: str | None = Field(
        default=None,
        description=(
            "Deterministic retrieval surface (ADR-0018 Phase 2): split identifiers + "
            "docstring + signature + decorator + call + import names. Python-only, zero-LLM."
        ),
    )


class CodeEdgeDraft(ArtifactModel):
    """One knowledge_edge row by symbolic keys; source is always 'graphify'."""

    from_key: str = Field(min_length=1)
    to_key: str = Field(min_length=1)
    edge_type: CodeEdgeType
    confidence: float = Field(ge=0.0, le=1.0)


class GraphifyResult(ArtifactModel):
    artifacts: tuple[CodeArtifactDraft, ...] = ()
    edges: tuple[CodeEdgeDraft, ...] = ()


__all__ = [
    "CodeArtifactDraft",
    "CodeArtifactType",
    "CodeEdgeDraft",
    "CodeEdgeType",
    "GraphifyResult",
    "SymbolKind",
]
