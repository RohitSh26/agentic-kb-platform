"""Local-filesystem fetch backend: build a KB from a workspace directory, no cloud.

The first real `FetchBackend` (others ship as production connectors). It lists files
under a workspace root and reads their text from disk, so the whole build plane runs
locally with no network and no credentials. Path include/exclude filtering and
`acl_teams` stamping are applied by `FilteredFetchBackend` (config_loader), so this
backend just enumerates and reads — deterministically.
"""

from collections.abc import Iterable
from pathlib import Path

from agentic_kb_builder.connectors.config_loader import BackendFactory
from agentic_kb_builder.connectors.source_connector import FetchBackend
from agentic_kb_builder.domain.source_config import SourceSpec
from agentic_kb_builder.domain.source_records import SourceRef
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# Directories never worth walking; keeps enumeration cheap and deterministic.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".ruff_cache"}
)


class LocalFsBackend:
    """Enumerate + read files under `root`, presenting them as one source's refs."""

    def __init__(self, root: Path, spec: SourceSpec, *, version: str) -> None:
        self._root = root.resolve()
        self._spec = spec
        self._version = version

    async def list_sources(self) -> list[SourceRef]:
        repo = getattr(self._spec, "repo", None)
        branch = getattr(self._spec, "branch", None)
        refs = [
            SourceRef(
                source_type=self._spec.type,
                source_uri=path.as_uri(),
                source_version=self._version,
                repo=repo,
                branch=branch,
                path=str(path.relative_to(self._root)),
            )
            for path in self._iter_files()
        ]
        logger.info(
            "event=local_fs_listed source=%s root=%s files=%d",
            self._spec.name,
            self._root,
            len(refs),
        )
        return refs

    async def fetch_text(self, source: SourceRef) -> str:
        # path is repo-relative (set in list_sources); strict UTF-8 so hashes are stable.
        return (self._root / (source.path or "")).read_text(encoding="utf-8")

    def _iter_files(self) -> Iterable[Path]:
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            parts = path.relative_to(self._root).parts
            # Apply the skip check to EVERY component including the leaf filename,
            # so a root-level dotfile (e.g. `.env`, parts=('.env',)) is excluded —
            # not just dotfiles nested under a skipped directory.
            if any(part in _SKIP_DIRS or part.startswith(".") for part in parts):
                continue
            yield path


def local_fs_backend_factory(root: Path, *, version: str = "local") -> BackendFactory:
    """A BackendFactory (config_loader) bound to one workspace root for every source."""

    def factory(spec: SourceSpec, _token: str | None) -> FetchBackend:
        return LocalFsBackend(root, spec, version=version)

    return factory


__all__ = ["LocalFsBackend", "local_fs_backend_factory"]
