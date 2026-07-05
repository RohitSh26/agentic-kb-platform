"""Pre-flight validation of a loaded SourceConfig against the chosen backend.

Catches config mistakes BEFORE any fetch, and reports ALL of them at once instead
of failing one-at-a-time mid-build. Mode-aware:

  production — every remote source must carry auth (or be explicitly `public: true`),
               and every referenced auth.token_env must be set in the environment.
  local      — the --workspace must exist; source types the local backend cannot
               read (azure_wiki / ado_card) are flagged; github include globs that
               match no local file are flagged.

Pure and side-effect-free: the environment and workspace are passed in, and a list
of issues is returned. The caller (the build CLI) aborts on any ERROR. The schema
itself (pydantic) is already validated by load_source_config; this layer adds the
cross-cutting, environment-aware checks the schema cannot express.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agentic_kb_builder.connectors.config_loader import LOCALLY_FETCHABLE_SPEC_TYPES
from agentic_kb_builder.domain.source_config import (
    PathSelectSpec,
    SourceConfig,
    SourceSpec,
)

# Pruned from the local-match walk: never part of an ingested source, and walking
# them (especially .git / .venv / node_modules) is slow and noisy.
_PRUNE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".kb-local-search-index.json",
    }
)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    source: str  # the source `name`, or "" for a config-level issue
    message: str

    def render(self) -> str:
        where = f"source {self.source!r}: " if self.source else ""
        return f"[{self.severity.value}] {where}{self.message}"


def has_errors(issues: list[ValidationIssue]) -> bool:
    return any(issue.severity is Severity.ERROR for issue in issues)


def validate_source_config(
    config: SourceConfig,
    *,
    backend: str,
    environ: Mapping[str, str],
    workspace: Path | None = None,
) -> list[ValidationIssue]:
    """Return every config issue for the given backend (ERRORs block the build)."""
    issues: list[ValidationIssue] = []
    if backend == "local" and (workspace is None or not workspace.is_dir()):
        issues.append(
            ValidationIssue(Severity.ERROR, "", f"--workspace path does not exist: {workspace}")
        )
    for spec in config.sources:
        if not spec.enabled:
            continue
        if backend == "production":
            issues.extend(_validate_production_source(spec, environ))
        else:
            issues.extend(_validate_local_source(spec, workspace))
    return issues


def _validate_production_source(
    spec: SourceSpec, environ: Mapping[str, str]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if spec.auth is None:
        if not spec.public:
            issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    spec.name,
                    f"type {spec.type!r} has no auth.token_env — it would send no token, and a "
                    "private repo/org returns 404 (not 403). Add `auth: {token_env: <ENV>}`, or "
                    "set `public: true` if this source is genuinely public.",
                )
            )
        return issues
    token_env = spec.auth.token_env
    if not environ.get(token_env):
        issues.append(
            ValidationIssue(
                Severity.ERROR,
                spec.name,
                f"auth.token_env {token_env} is not set in the build environment "
                "(export it or `source` your .env before building).",
            )
        )
    if spec.public:
        issues.append(
            ValidationIssue(
                Severity.WARNING,
                spec.name,
                "marked `public: true` but also sets auth — the token will be sent anyway.",
            )
        )
    return issues


def _validate_local_source(spec: SourceSpec, workspace: Path | None) -> list[ValidationIssue]:
    if not isinstance(spec, LOCALLY_FETCHABLE_SPEC_TYPES):
        return [
            ValidationIssue(
                Severity.WARNING,
                spec.name,
                f"type {spec.type!r} is not fetchable by --backend local (it reads workspace "
                "files only) and will be skipped — use --backend production to ingest it.",
            )
        ]
    if (
        workspace is not None
        and workspace.is_dir()
        and isinstance(spec, PathSelectSpec)
        and not _any_local_match(workspace, spec)
    ):
        return [
            ValidationIssue(
                Severity.WARNING,
                spec.name,
                f"include globs {spec.include} match no files under {workspace} — "
                "check the paths (build will produce nothing for this source).",
            )
        ]
    return []


def _any_local_match(workspace: Path, spec: PathSelectSpec) -> bool:
    """True as soon as one workspace file passes the source's include/exclude filter.

    Early-exit keeps a valid config cheap; heavy/irrelevant dirs are pruned so a large
    checkout doesn't make the pre-flight crawl.
    """
    path_filter = spec.path_filter()
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        rel_root = os.path.relpath(root, workspace)
        for name in files:
            rel = name if rel_root == "." else f"{rel_root}/{name}"
            if path_filter.matches(rel):
                return True
    return False


__all__ = [
    "Severity",
    "ValidationIssue",
    "has_errors",
    "validate_source_config",
]
