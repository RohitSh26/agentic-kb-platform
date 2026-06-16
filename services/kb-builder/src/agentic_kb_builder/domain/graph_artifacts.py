"""Graphify output shapes: parsed code graphs and canonical code artifacts/edges.

Graphify is the code-structure layer (architecture §5): a navigation aid, not
final evidence. Symbols carry exact spans so L2 evidence can return precise
snippets at a source version. Edge drafts use symbolic keys (resolved to
artifact uuids only after persistence): "file:{path}", "sym:{path}::{name}",
"test:{path}::{name}", "endpoint:{path}::{method} {route}".
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from agentic_kb_builder.domain.artifact_model import ArtifactModel

CodeArtifactType = Literal["code_file", "code_symbol", "endpoint", "test"]
CodeEdgeType = Literal["imports", "calls", "tests", "exposed_as", "defined_in"]
SymbolKind = Literal["function", "class", "method"]


class _Spanned(ArtifactModel):
    """1-based inclusive line span within the file."""

    span_start: int = Field(ge=1)
    span_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _span_ordered(self) -> Self:
        if self.span_end < self.span_start:
            raise ValueError(f"span_end {self.span_end} < span_start {self.span_start}")
        return self


class ParsedSymbol(_Spanned):
    name: str = Field(min_length=1)
    kind: SymbolKind


class ParsedEndpoint(ArtifactModel):
    http_method: str = Field(min_length=1)
    route: str = Field(min_length=1)
    symbol: str = Field(min_length=1, description="handler symbol name in this file")


class ParsedTest(_Spanned):
    name: str = Field(min_length=1)
    targets: tuple[str, ...] = Field(
        default=(),
        description="symbol names this test exercises; cross-file as 'path::name'",
    )


class ParsedImport(ArtifactModel):
    target_path: str = Field(
        min_length=1, description="repo-relative path of the imported file, pre-resolved"
    )


class ParsedCall(ArtifactModel):
    from_symbol: str = Field(min_length=1)
    to_symbol: str = Field(min_length=1, description="same-file name or cross-file 'path::name'")


class FileGraph(ArtifactModel):
    """Parsed Graphify output for exactly one code file at one commit."""

    path: str = Field(min_length=1)
    symbols: tuple[ParsedSymbol, ...] = ()
    endpoints: tuple[ParsedEndpoint, ...] = ()
    tests: tuple[ParsedTest, ...] = ()
    imports: tuple[ParsedImport, ...] = ()
    calls: tuple[ParsedCall, ...] = ()


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
    "FileGraph",
    "GraphifyResult",
    "ParsedCall",
    "ParsedEndpoint",
    "ParsedImport",
    "ParsedSymbol",
    "ParsedTest",
    "SymbolKind",
]
