"""In-memory view of a knowledge artifact as the linker sees it."""

import uuid
from dataclasses import dataclass

CODE_ARTIFACT_TYPES = frozenset({"code_file", "code_symbol", "endpoint"})
DOC_SOURCE_TYPES = frozenset({"azure_wiki", "github_doc"})
CARD_SOURCE_TYPES = frozenset({"ado_card"})
DOC_ARTIFACT_TYPES = frozenset({"chunk", "summary", "source_backed_fact"})
# Cross-domain (PR-26): commit artifacts come from git_metadata sources, and
# work-item artifacts are derived from ado_card sources.
COMMIT_ARTIFACT_TYPE = "commit"
COMMIT_SOURCE_TYPES = frozenset({"git_metadata"})
WORK_ITEM_SOURCE_TYPES = CARD_SOURCE_TYPES


@dataclass(frozen=True)
class LinkableArtifact:
    artifact_id: uuid.UUID
    artifact_type: str
    title: str | None
    body_text: str | None
    source_type: str
    # cross-domain (PR-26): a card's work-item id (source_item.external_id), a
    # code/commit artifact's source path (source_item.path), and a commit's
    # branch (source_item.branch) — loaded so the deterministic cross-domain
    # rules can match references, changed files, and branch-name work items.
    external_id: str | None = None
    path: str | None = None
    branch: str | None = None
