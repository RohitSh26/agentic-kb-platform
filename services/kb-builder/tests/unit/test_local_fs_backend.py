"""LocalFsBackend enumeration filtering (KB-F5 secret-leak regression).

The dotfile/skip filter must apply to EVERY path component including the leaf
filename, so a root-level dotfile (e.g. `.env`) is never enumerated or read —
not just dotfiles nested under a skipped directory.
"""

from pathlib import Path

import pytest

from agentic_kb_builder.connectors.local_fs import LocalFsBackend
from agentic_kb_builder.domain.source_config import GithubCodeSourceSpec


def _spec() -> GithubCodeSourceSpec:
    return GithubCodeSourceSpec(name="ws", type="github_code", repo="org/repo")


async def _list_paths(root: Path) -> list[str]:
    backend = LocalFsBackend(root, _spec(), version="local")
    refs = await backend.list_sources()
    return sorted(ref.path or "" for ref in refs)


@pytest.mark.asyncio
async def test_root_level_dotfile_is_not_yielded(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=leak\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")

    paths = await _list_paths(tmp_path)

    assert ".env" not in paths
    assert ".gitignore" not in paths
    assert "app.py" in paths


@pytest.mark.asyncio
async def test_nested_dotfile_and_skip_dirs_still_filtered(tmp_path: Path) -> None:
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / ".env").write_text("SECRET=leak\n", encoding="utf-8")
    (nested / "mod.py").write_text("x = 1\n", encoding="utf-8")
    skipped = tmp_path / "node_modules"
    skipped.mkdir()
    (skipped / "lib.js").write_text("//\n", encoding="utf-8")

    paths = await _list_paths(tmp_path)

    assert paths == ["pkg/mod.py"]
