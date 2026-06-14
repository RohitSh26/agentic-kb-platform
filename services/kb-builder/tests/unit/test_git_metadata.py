"""git_metadata connector: deterministic rendering + changed-file roundtrip (PR-26).

Builds a tiny throwaway git repo in a temp dir (no network, no workspace
mutation) and asserts the connector is deterministic: same source state ⇒ same
rendering ⇒ same content_hash, and the changed-file section roundtrips through
parse_changed_files so the linker can recover paths from body_text.
"""

import subprocess
from pathlib import Path

import pytest

from agentic_kb_builder.connectors.git_metadata import (
    CHANGED_FILES_HEADER,
    GitMetadataConnector,
    parse_changed_files,
    read_commits,
    render_commit,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.test")
    _git(root, "config", "user.name", "Tester")
    _git(root, "checkout", "-q", "-b", "feature/AB-321-thing")
    (root / "src").mkdir()
    (root / "src" / "service.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "AB#321 add service\n\nimplements the thing")
    return root


def test_read_commits_is_deterministic_and_bounded(repo: Path) -> None:
    first = read_commits(repo, max_commits=10)
    second = read_commits(repo, max_commits=10)
    assert len(first) == 1
    assert first == second
    commit = first[0]
    assert commit.subject == "AB#321 add service"
    assert "implements the thing" in commit.body
    assert commit.changed_files == ("README.md", "src/service.py")


def test_max_commits_zero_yields_nothing(repo: Path) -> None:
    assert read_commits(repo, max_commits=0) == []


def test_render_commit_roundtrips_changed_files(repo: Path) -> None:
    (commit,) = read_commits(repo, max_commits=1)
    rendering = render_commit(commit)
    assert CHANGED_FILES_HEADER in rendering
    assert parse_changed_files(rendering) == ("README.md", "src/service.py")


def test_same_state_same_hash(repo: Path) -> None:
    (commit,) = read_commits(repo, max_commits=1)
    assert render_commit(commit) == render_commit(commit)


async def test_connector_lists_and_fetches_deterministically(repo: Path) -> None:
    connector = GitMetadataConnector(repo, max_commits=10)
    refs = await connector.list_sources()
    assert len(refs) == 1
    ref = refs[0]
    assert ref.source_type == "git_metadata"
    assert ref.source_uri == f"git:{ref.source_version}"
    assert ref.branch == "feature/AB-321-thing"
    assert ref.path is None

    first = await connector.fetch(ref)
    second = await connector.fetch(ref)
    assert first.content_hash == second.content_hash
    assert parse_changed_files(first.text) == ("README.md", "src/service.py")
