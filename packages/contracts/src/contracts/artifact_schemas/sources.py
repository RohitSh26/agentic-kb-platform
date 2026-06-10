"""Source identity and normalized-content shapes shared by all connectors."""

from typing import Literal

from contracts.artifact_schemas import ArtifactModel

SourceType = Literal["github_code", "github_doc", "azure_wiki", "ado_card"]


class SourceRef(ArtifactModel):
    """Identity of one source item; mirrors the source_item registry columns.

    source_version semantics per connector: github_code/github_doc = commit SHA,
    azure_wiki = page revision (page id goes in external_id, never concatenated),
    ado_card = revision.
    """

    source_type: SourceType
    source_uri: str
    source_version: str
    repo: str | None = None
    branch: str | None = None
    path: str | None = None
    external_id: str | None = None


class NormalizedContent(ArtifactModel):
    """Deterministically normalized text plus its content_hash for one source.

    Same source state must always produce the same text and hash, on any machine.
    """

    source: SourceRef
    text: str
    content_hash: str


__all__ = ["NormalizedContent", "SourceRef", "SourceType"]
