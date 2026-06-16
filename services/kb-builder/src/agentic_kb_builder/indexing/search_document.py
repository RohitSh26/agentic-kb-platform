"""Search projection contract (PR-08, architecture §1/§6/§14, ADR-0002).

The Azure AI Search index is a derived, rebuildable projection of the Postgres
Knowledge Registry — never truth. A SearchDoc is the unit of that projection:
one document per projectable artifact, keyed by the artifact's UUID so a
rebuild from Postgres produces byte-identical doc identities. artifact_hash is
carried so post-build consistency validation can compare index state against
the registry without fetching bodies.

Only artifacts meant to rank by their own text are projected (concept, summary,
chunk, source_backed_fact, code_symbol, commit). `code_file` and `endpoint` are
pointer-only (body_text=None). `test` carries a snippet body like code_symbol
but is deliberately kept out of the index — a test is reached through its graph
edges to what it exercises, not by ranking its body. `commit` is a deterministic
git-metadata artifact (zero-LLM) projected by its own body text so commits are
directly searchable.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

SEARCH_SCHEMA_VERSION = "1.0.0"

PROJECTABLE_ARTIFACT_TYPES = frozenset(
    {"concept", "summary", "chunk", "source_backed_fact", "code_symbol", "commit"}
)


class SearchModel(BaseModel):
    """Base for search projection schemas: frozen, versioned, no extras."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SEARCH_SCHEMA_VERSION


class SearchDoc(SearchModel):
    """One hybrid (BM25 + vector) index document, derived from one artifact."""

    doc_id: str
    artifact_id: uuid.UUID
    artifact_type: str
    source_type: str
    source_uri: str
    title: str | None
    body_text: str | None
    kb_version: str
    knowledge_kind: str | None
    authority_score: float | None
    freshness_score: float | None
    artifact_hash: str | None
    # Deterministic retrieval surface for code_symbol artifacts (ADR-0018 Phase 2).
    # None for prose artifacts. The MCP-server keyword scorer ranks on this field
    # alongside title / body_text so concept-word queries hit even when the raw body
    # doesn't contain the searched word.
    search_text: str | None = None
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None

    @staticmethod
    def doc_id_for(artifact_id: uuid.UUID) -> str:
        """Stable doc key: rebuilding from Postgres reproduces identities."""
        return str(artifact_id)


class IndexState(SearchModel):
    """Snapshot of index contents used by the drift consistency check."""

    docs: dict[str, str | None] = Field(default_factory=dict)
    """doc_id -> artifact_hash as stored in the index (None when unhashed)."""


__all__ = [
    "PROJECTABLE_ARTIFACT_TYPES",
    "SEARCH_SCHEMA_VERSION",
    "IndexState",
    "SearchDoc",
    "SearchModel",
]
