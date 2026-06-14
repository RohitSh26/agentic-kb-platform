"""Deterministic cross-domain link rules (PR-26, ADR-0010 phase 2).

Zero LLM, exact references only — no fuzzy/partial matches (the brief's explicit
"Do NOT"). Three rules, all EXTRACTED, source='linker', strategy='deterministic',
each with an evidence pointer and relation_schema_version=1:

1. implements (commit → work-item): parse explicit work-item references from the
   commit message + branch name (AB#123, #123, GH-123, PR #123) and match them to
   a work-item artifact by external_id or title. Bare commit SHAs are recognised
   too. The evidence pointer is the exact matched substring.
2. mentions (commit → code_file): for each changed file path in the commit, link
   the commit to the code_file artifact whose source path equals that path
   (exact). The evidence pointer is the changed-file path.
3. mentions (doc → work-item): a doc that names a work-item id verbatim links to
   the work-item artifact (extends the existing doc-scan; same precision guard —
   the reference must be an explicit AB#/#/GH- form, never a bare incidental
   number).

Negative discipline: a bare integer that is NOT in an explicit reference form
(e.g. "fixed 42 tests", a version "1.2.3", a year) never produces a link.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agentic_kb_builder.connectors.git_metadata import parse_changed_files
from agentic_kb_builder.domain import LinkEdgeDraft
from agentic_kb_builder.linker.records import (
    COMMIT_ARTIFACT_TYPE,
    WORK_ITEM_SOURCE_TYPES,
    LinkableArtifact,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

IMPLEMENTS_CONFIDENCE = 0.95
MENTIONS_CONFIDENCE = 0.95

# Explicit work-item reference forms. Each captures the numeric id in group
# "id" and the whole match is the evidence pointer. A bare number is NEVER a
# reference — it must carry one of these explicit prefixes.
_WORK_ITEM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AB#(?P<id>\d+)"),  # Azure Boards: AB#123
    re.compile(r"GH-(?P<id>\d+)"),  # GitHub-style: GH-123
    re.compile(r"PR\s*#(?P<id>\d+)", re.IGNORECASE),  # PR #123 / PR#123
    re.compile(r"(?<![\w#-])#(?P<id>\d+)"),  # bare #123 (not part of AB#/PR#)
)
# Branch tokens like feature/AB-123-foo or bugfix/123-foo carry a work-item id.
# Lookarounds keep group(0) equal to the matched reference (the evidence pointer)
# so the surrounding "/" and "-" separators never leak into it.
_BRANCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AB-(?P<id>\d+)"),
    re.compile(r"GH-(?P<id>\d+)"),
    re.compile(r"(?<=/)(?P<id>\d+)(?=[-_/]|$)"),
    re.compile(r"(?<=^)(?P<id>\d+)(?=[-_/]|$)"),
)
# A 7-40 char lowercase hex run, boundary-guarded, is a commit SHA reference.
# The `(?=[0-9a-f]*[a-f])` lookahead requires at least one a-f letter so a bare
# decimal run (a year, a row count, an issue number) is never taken for a SHA.
_SHA_PATTERN = re.compile(r"(?<![\w])(?=[0-9a-f]*[a-f])(?P<sha>[0-9a-f]{7,40})(?![\w])")


@dataclass(frozen=True)
class _Reference:
    """One parsed reference: the numeric/sha key and the exact matched text."""

    key: str
    matched: str


def parse_work_item_references(message: str, branch: str | None) -> list[_Reference]:
    """Parse explicit work-item references from a commit message + branch name.

    Deterministic and exact: only the explicit forms above match; an incidental
    bare number never does. Returns each unique (numeric id, matched substring).
    """
    refs: dict[str, _Reference] = {}
    for pattern in _WORK_ITEM_PATTERNS:
        for match in pattern.finditer(message):
            key = match.group("id")
            refs.setdefault(key, _Reference(key=key, matched=match.group(0)))
    if branch:
        for pattern in _BRANCH_PATTERNS:
            for match in pattern.finditer(branch):
                key = match.group("id")
                refs.setdefault(key, _Reference(key=key, matched=match.group(0)))
    return list(refs.values())


def parse_sha_references(message: str) -> list[_Reference]:
    """Parse bare commit-SHA references (7-40 hex) from a commit message."""
    refs: dict[str, _Reference] = {}
    for match in _SHA_PATTERN.finditer(message):
        sha = match.group("sha")
        refs.setdefault(sha, _Reference(key=sha, matched=sha))
    return list(refs.values())


def _work_item_index(
    artifacts: Sequence[LinkableArtifact],
) -> dict[str, LinkableArtifact]:
    """Index work-item artifacts by every key they can be matched on:
    external_id, the numeric tail of the title, and the title's digit run."""
    index: dict[str, LinkableArtifact] = {}
    for artifact in artifacts:
        if artifact.source_type not in WORK_ITEM_SOURCE_TYPES:
            continue
        for key in _work_item_keys(artifact):
            index.setdefault(key, artifact)
    return index


def _work_item_keys(artifact: LinkableArtifact) -> set[str]:
    keys: set[str] = set()
    if artifact.external_id:
        keys.add(artifact.external_id.strip())
        # an external_id like "AB#1234" or "1234" both yield the numeric id
        digits = re.findall(r"\d+", artifact.external_id)
        keys.update(digits)
    if artifact.title:
        keys.update(re.findall(r"\d+", artifact.title))
    return {key for key in keys if key}


def find_cross_domain_links(
    artifacts: Sequence[LinkableArtifact],
) -> list[LinkEdgeDraft]:
    """The three deterministic cross-domain rules. Returns LinkEdgeDrafts with an
    evidence pointer and strategy='deterministic'; the caller dedupes/writes."""
    commits = [a for a in artifacts if a.artifact_type == COMMIT_ARTIFACT_TYPE]
    work_items = _work_item_index(artifacts)
    sha_index = {a.external_id: a for a in commits if a.external_id}
    code_files = {
        a.path: a for a in artifacts if a.artifact_type == "code_file" and a.path is not None
    }

    drafts: list[LinkEdgeDraft] = []
    seen: set[tuple[object, object, str]] = set()

    def add(
        from_a: LinkableArtifact,
        to_a: LinkableArtifact,
        edge_type: str,
        conf: float,
        evidence: dict[str, str],
    ) -> None:
        key = (from_a.artifact_id, to_a.artifact_id, edge_type)
        if key in seen or from_a.artifact_id == to_a.artifact_id:
            return
        seen.add(key)
        drafts.append(
            LinkEdgeDraft(
                from_artifact_id=from_a.artifact_id,
                to_artifact_id=to_a.artifact_id,
                edge_type=edge_type,  # type: ignore[arg-type]
                confidence=conf,
                strategy="deterministic",
                evidence=evidence,
            )
        )

    implements = 0
    file_mentions = 0
    for commit in commits:
        message = commit.body_text or ""
        # Rule 1: implements (commit → work-item) from explicit references.
        for ref in parse_work_item_references(message, commit.branch):
            target = work_items.get(ref.key)
            if target is not None:
                add(
                    commit,
                    target,
                    "implements",
                    IMPLEMENTS_CONFIDENCE,
                    {"kind": "work_item_ref", "matched": ref.matched},
                )
                implements += 1
        # SHA references that resolve to another known commit artifact (implements).
        for ref in parse_sha_references(message):
            target_commit = _resolve_sha(ref.key, sha_index)
            if target_commit is not None and target_commit.artifact_id != commit.artifact_id:
                add(
                    commit,
                    target_commit,
                    "implements",
                    IMPLEMENTS_CONFIDENCE,
                    {"kind": "commit_ref", "matched": ref.matched},
                )
                implements += 1
        # Rule 2: mentions (commit → code_file) from changed file paths.
        for path in parse_changed_files(commit.body_text or ""):
            code_file = code_files.get(path)
            if code_file is not None:
                add(
                    commit,
                    code_file,
                    "mentions",
                    MENTIONS_CONFIDENCE,
                    {"kind": "changed_file", "path": path},
                )
                file_mentions += 1

    logger.info(
        "event=linker_cross_domain_matched commits=%d work_items=%d code_files=%d "
        "implements=%d file_mentions=%d edges=%d",
        len(commits),
        len(work_items),
        len(code_files),
        implements,
        file_mentions,
        len(drafts),
    )
    return drafts


def find_doc_work_item_mentions(
    docs: Sequence[LinkableArtifact],
    artifacts: Sequence[LinkableArtifact],
) -> list[LinkEdgeDraft]:
    """Rule 3: a doc that names a work-item id verbatim ⇒ mentions (doc → work-item).

    Uses the same explicit-reference parser as rule 1, so a bare incidental
    number in a doc never links — only AB#/#/GH-/PR# forms.
    """
    work_items = _work_item_index(artifacts)
    drafts: list[LinkEdgeDraft] = []
    seen: set[tuple[object, object, str]] = set()
    for doc in docs:
        for ref in parse_work_item_references(doc.body_text or "", None):
            target = work_items.get(ref.key)
            if target is None or target.artifact_id == doc.artifact_id:
                continue
            key = (doc.artifact_id, target.artifact_id, "mentions")
            if key in seen:
                continue
            seen.add(key)
            drafts.append(
                LinkEdgeDraft(
                    from_artifact_id=doc.artifact_id,
                    to_artifact_id=target.artifact_id,
                    edge_type="mentions",
                    confidence=MENTIONS_CONFIDENCE,
                    strategy="deterministic",
                    evidence={"kind": "work_item_ref", "matched": ref.matched},
                )
            )
    return drafts


def _resolve_sha(sha: str, sha_index: dict[str, LinkableArtifact]) -> LinkableArtifact | None:
    # Exact full-SHA match first; then unique prefix match (7+ hex is unambiguous
    # in practice but we require uniqueness to stay deterministic).
    exact = sha_index.get(sha)
    if exact is not None:
        return exact
    matches = [a for key, a in sha_index.items() if key.startswith(sha)]
    return matches[0] if len(matches) == 1 else None


__all__ = [
    "IMPLEMENTS_CONFIDENCE",
    "MENTIONS_CONFIDENCE",
    "find_cross_domain_links",
    "find_doc_work_item_mentions",
    "parse_sha_references",
    "parse_work_item_references",
]
