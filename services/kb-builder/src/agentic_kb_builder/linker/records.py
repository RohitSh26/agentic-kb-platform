"""In-memory view of a knowledge artifact as the linker sees it."""

import uuid
from dataclasses import dataclass

CODE_ARTIFACT_TYPES = frozenset({"code_file", "code_symbol", "endpoint"})
DOC_SOURCE_TYPES = frozenset({"azure_wiki", "github_doc"})
CARD_SOURCE_TYPES = frozenset({"ado_card"})
DOC_ARTIFACT_TYPES = frozenset({"chunk", "summary", "source_backed_fact"})


@dataclass(frozen=True)
class LinkableArtifact:
    artifact_id: uuid.UUID
    artifact_type: str
    title: str | None
    body_text: str | None
    source_type: str
