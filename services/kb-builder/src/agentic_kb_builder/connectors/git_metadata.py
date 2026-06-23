"""Local git-metadata connector: one deterministic commit artifact per commit.

Reads the LOCAL git repo at the workspace root (no GitHub/ADO API — that is the
production track) by shelling out to `git log` with a stable, machine-readable
format and parsing stdout. For each commit it produces:

- a SourceRef (source_type='git_metadata', source_uri=`git:<sha>`,
  source_version=full SHA, branch=current branch) — deterministic identity, and
- a normalized rendering = subject + body + a clearly delimited, sorted list of
  changed file paths.

Same source state ⇒ same rendering ⇒ same content_hash (connectors rule), so an
unchanged commit is skipped on the next build. History is bounded by
`max_commits` so the scan is cheap and deterministic. Commits are zero-LLM:
they skip extraction and graphify entirely (PR-26 is "deterministic, no LLM").

The changed-file section is delimited so the linker can recover the file list
from body_text deterministically (see `parse_changed_files`).
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agentic_kb_builder.domain.content_hasher import content_hash, normalize_text
from agentic_kb_builder.domain.source_records import NormalizedContent, SourceRef, SourceType
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_COMMITS = 200

# Field/record separators chosen to never collide with commit text. git fills
# %x1f / %x1e with the literal control bytes, so the parse stays unambiguous.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_LOG_FORMAT = f"%H{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}"

# Delimits the sorted changed-file section inside the commit artifact body so the
# linker can recover the exact file list deterministically (parse_changed_files).
CHANGED_FILES_HEADER = "--- changed files ---"


@dataclass(frozen=True)
class CommitRecord:
    """One parsed commit: identity + message + sorted changed file paths."""

    sha: str
    subject: str
    body: str
    changed_files: tuple[str, ...]


def _run_git(root: Path, args: list[str]) -> str:
    """Run a git command under `root`; raise on failure (no silent failures)."""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        # Decode as UTF-8 explicitly (not the locale's preferred encoding) so the
        # same repo renders byte-identical on any machine — same state ⇒ same
        # content_hash (connectors rule).
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def is_git_work_tree(root: Path) -> bool:
    """True iff `root` is inside a git work tree. A non-repo workspace is a
    valid configuration (no commit artifacts) — never an error."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _parse_log_record(record: str) -> tuple[str, str, str]:
    """Split one `git log` record into (sha, subject, body); fields are blank
    when git emitted none. Deterministic — pure string parse."""
    fields = [*record.lstrip("\n").split(_FIELD_SEP), "", "", ""]
    return fields[0].strip(), fields[1].strip(), fields[2].strip()


def _current_branch(root: Path) -> str | None:
    branch = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    # detached HEAD reports "HEAD"; record None rather than a misleading label.
    return None if branch in ("", "HEAD") else branch


def _changed_files(root: Path, sha: str) -> tuple[str, ...]:
    # --no-renames keeps the path set deterministic; root commit (no parent) has
    # no diff and yields an empty list, which the renderer handles.
    out = _run_git(
        root,
        ["show", "--name-only", "--no-color", "--no-renames", "--pretty=format:", sha],
    )
    paths = {line.strip() for line in out.splitlines() if line.strip()}
    return tuple(sorted(paths))


def read_commits(root: Path, *, max_commits: int = DEFAULT_MAX_COMMITS) -> list[CommitRecord]:
    """Read up to `max_commits` commits from HEAD, newest first, deterministically."""
    if max_commits < 1:
        return []
    raw = _run_git(
        root,
        ["log", f"--max-count={max_commits}", "--no-color", f"--pretty=format:{_LOG_FORMAT}"],
    )
    commits: list[CommitRecord] = []
    for record in raw.split(_RECORD_SEP):
        if not record.strip():
            continue
        sha, subject, body = _parse_log_record(record)
        if not sha:
            continue
        commits.append(
            CommitRecord(
                sha=sha,
                subject=subject,
                body=body,
                changed_files=_changed_files(root, sha),
            )
        )
    return commits


def render_commit(commit: CommitRecord) -> str:
    """Deterministic normalized rendering: subject + body + delimited sorted
    changed-file list. Stable across machines (same state ⇒ same text ⇒ hash)."""
    sections = [commit.subject]
    if commit.body:
        sections.append(commit.body)
    files_block = "\n".join((CHANGED_FILES_HEADER, *commit.changed_files))
    sections.append(files_block)
    return normalize_text("\n\n".join(section for section in sections if section))


def parse_changed_files(body_text: str) -> tuple[str, ...]:
    """Recover the changed-file paths a commit artifact's body_text encodes.

    Deterministic inverse of `render_commit`'s changed-file section — the linker
    reads this to resolve changed-file → code edges without a separate column.

    `render_commit` always appends the changed-file section LAST, so we scan from
    the end: a commit message whose own text contains a line equal to the header
    can no longer poison the recovered file list.
    """
    lines = body_text.splitlines()
    header_indices = [i for i, line in enumerate(lines) if line.strip() == CHANGED_FILES_HEADER]
    if not header_indices:
        return ()
    start = header_indices[-1]
    return tuple(line.strip() for line in lines[start + 1 :] if line.strip())


class GitMetadataConnector:
    """Enumerate local commits as git_metadata sources; fetch returns the
    deterministic per-commit rendering + content_hash.

    Mirrors the Connector protocol (list_sources / fetch) but does its own
    deterministic normalize+hash — it has no per-file FetchBackend, it shells out
    to git under the workspace root.
    """

    source_type: ClassVar[SourceType] = "git_metadata"

    def __init__(
        self, root: Path, *, branch: str | None = None, max_commits: int = DEFAULT_MAX_COMMITS
    ) -> None:
        self._root = root.resolve()
        self._max_commits = max_commits
        self._branch = branch
        self._records: dict[str, CommitRecord] = {}

    async def list_sources(self) -> list[SourceRef]:
        if not is_git_work_tree(self._root):
            logger.info(
                "event=git_metadata_not_a_repo root=%s reason=no_git_work_tree",
                self._root,
            )
            return []
        branch = self._branch if self._branch is not None else _current_branch(self._root)
        commits = read_commits(self._root, max_commits=self._max_commits)
        self._records = {commit.sha: commit for commit in commits}
        refs = [
            SourceRef(
                source_type="git_metadata",
                source_uri=f"git:{commit.sha}",
                source_version=commit.sha,
                branch=branch,
                path=None,
                external_id=commit.sha,
            )
            for commit in commits
        ]
        logger.info(
            "event=git_metadata_listed root=%s branch=%s commits=%d max_commits=%d",
            self._root,
            branch,
            len(refs),
            self._max_commits,
        )
        return refs

    async def fetch(self, source: SourceRef) -> NormalizedContent:
        commit = self._records.get(source.source_version)
        if commit is None:
            # list_sources populates the cache; a fetch for an unknown sha means a
            # bare-ref call — re-read just this commit deterministically.
            commit = self._read_single(source.source_version)
        text = render_commit(commit)
        digest = content_hash(text)
        logger.info(
            "event=git_metadata_fetch sha=%s changed_files=%d content_hash=%s",
            commit.sha,
            len(commit.changed_files),
            digest,
        )
        return NormalizedContent(source=source, text=text, content_hash=digest)

    def _read_single(self, sha: str) -> CommitRecord:
        raw = _run_git(
            self._root, ["log", "-1", "--no-color", f"--pretty=format:{_LOG_FORMAT}", sha]
        )
        parsed_sha, subject, body = _parse_log_record(raw.split(_RECORD_SEP)[0])
        return CommitRecord(
            sha=parsed_sha,
            subject=subject,
            body=body,
            changed_files=_changed_files(self._root, sha),
        )


__all__ = [
    "CHANGED_FILES_HEADER",
    "DEFAULT_MAX_COMMITS",
    "CommitRecord",
    "GitMetadataConnector",
    "is_git_work_tree",
    "parse_changed_files",
    "read_commits",
    "render_commit",
]
