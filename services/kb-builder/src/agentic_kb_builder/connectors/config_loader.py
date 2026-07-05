"""Load sources.yaml into typed specs and construct connectors.

Fail-fast pipeline: parse YAML -> validate schema -> resolve every configured
token_env against the environment (only when the selected backend actually
authenticates; see `connectors_from_config`'s `authenticates` flag) -> construct
connectors. Any failure aborts before a single fetch. Token values exist only
as local variables handed to the backend factory — never on a model, never in
a log.
"""

import os
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from agentic_kb_builder.connectors.ado_card import AdoCardConnector
from agentic_kb_builder.connectors.azure_wiki import AzureWikiConnector
from agentic_kb_builder.connectors.github_code import GitHubCodeConnector
from agentic_kb_builder.connectors.github_doc import GitHubDocConnector
from agentic_kb_builder.connectors.source_connector import (
    BaseConnector,
    Connector,
    FetchBackend,
)
from agentic_kb_builder.domain import SourceRef, SourceType
from agentic_kb_builder.domain.source_config import (
    GithubCodeSourceSpec,
    GithubDocSourceSpec,
    PathFilter,
    PathSelectSpec,
    SourceConfig,
    SourceSpec,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

SOURCE_CONFIG_PATH_ENV = "SOURCE_CONFIG_PATH"

BackendFactory = Callable[[SourceSpec, str | None], FetchBackend]

_CONNECTOR_TYPES: dict[SourceType, type[BaseConnector]] = {
    "github_code": GitHubCodeConnector,
    "github_doc": GitHubDocConnector,
    "azure_wiki": AzureWikiConnector,
    "ado_card": AdoCardConnector,
}

# Source spec types the local-filesystem backend can genuinely serve: it reads
# workspace files by path, so only the path-selecting GitHub sources are fetchable.
# azure_wiki/ado_card have no local representation at all — without this filter the
# local backend's default PathFilter (matches "**") would treat every workspace file
# as though it were a wiki page or card. This is the single source of truth for
# local-fetchability; config_validator's pre-flight warning uses the same constant so
# the two can never drift on a new source type.
LOCALLY_FETCHABLE_SPEC_TYPES: Final = (GithubCodeSourceSpec, GithubDocSourceSpec)


class SourceConfigError(Exception):
    """Invalid or unloadable source configuration; aborts the build."""


def _format_validation_error(path: Path, data: dict[str, object], exc: ValidationError) -> str:
    sources = data.get("sources")
    lines = [f"invalid source config {path}:"]
    for error in exc.errors():
        loc = error["loc"]
        prefix = ""
        if len(loc) > 1 and loc[0] == "sources" and isinstance(loc[1], int):
            name: object = None
            if isinstance(sources, list) and loc[1] < len(sources):
                entry = sources[loc[1]]
                if isinstance(entry, dict):
                    name = entry.get("name")
            prefix = f"source {name!r} " if isinstance(name, str) else f"source #{loc[1]} "
        dotted = ".".join(str(part) for part in loc)
        lines.append(f"  {prefix}at {dotted}: {error['msg']}")
    return "\n".join(lines)


def load_source_config(path: str | Path) -> SourceConfig:
    """Parse and validate a sources.yaml; any failure raises SourceConfigError
    naming the file and, where possible, the offending source."""
    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceConfigError(f"cannot read source config {config_path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SourceConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceConfigError(f"{config_path}: top level must be a mapping")
    try:
        config = SourceConfig.model_validate(data)
    except ValidationError as exc:
        raise SourceConfigError(_format_validation_error(config_path, data, exc)) from exc
    counts = Counter(spec.type for spec in config.sources)
    # Generic per-type breakdown: a new SourceType needs no edit here (Open/Closed).
    by_type = " ".join(f"{t}={n}" for t, n in sorted(counts.items()))
    logger.info(
        "event=source_config_loaded path=%s sources=%d enabled=%d by_type=%s",
        config_path,
        len(config.sources),
        sum(1 for spec in config.sources if spec.enabled),
        by_type or "none",
    )
    return config


def resolve_token(spec: SourceSpec) -> str | None:
    """Resolve the configured token_env to its value, or None when the source
    is unauthenticated. A configured-but-unset variable is a hard error."""
    if spec.auth is None:
        return None
    value = os.environ.get(spec.auth.token_env)
    if value is None:
        raise SourceConfigError(
            f"source {spec.name!r}: auth.token_env {spec.auth.token_env} "
            "is not set in the environment"
        )
    return value


class FilteredFetchBackend:
    """Wraps any FetchBackend: drops refs whose path fails the include/exclude
    filter (an excluded path is never fetched, hashed, or stored) and stamps
    the source's acl_teams onto every surviving ref."""

    def __init__(
        self,
        inner: FetchBackend,
        path_filter: PathFilter,
        acl_teams: Sequence[str],
        *,
        source_name: str,
    ) -> None:
        self._inner = inner
        self._path_filter = path_filter
        self._acl_teams = list(acl_teams)
        self._source_name = source_name

    async def list_sources(self) -> list[SourceRef]:
        listed = await self._inner.list_sources()
        kept: list[SourceRef] = []
        for ref in listed:
            # path=None (card-shaped refs) bypasses the glob filter by design;
            # path-selecting connectors must always emit a path
            if ref.path is not None and not self._path_filter.matches(ref.path):
                continue
            kept.append(ref.model_copy(update={"acl_teams": list(self._acl_teams)}))
        logger.info(
            "event=fetch_backend_filtered source=%s listed=%d kept=%d excluded=%d",
            self._source_name,
            len(listed),
            len(kept),
            len(listed) - len(kept),
        )
        return kept

    async def fetch_text(self, source: SourceRef) -> str:
        return await self._inner.fetch_text(source)


def connectors_from_config(
    config: SourceConfig,
    backend_factory: BackendFactory,
    *,
    authenticates: bool = True,
    locally_fetchable_only: bool = False,
) -> list[Connector]:
    """Construct one connector per enabled, fetchable source. Tokens are resolved
    here — for every enabled, fetchable source, before any connector runs — and
    handed to the backend factory as a local value only.

    Two flags, both driven purely by the selected backend (never by the source
    config itself):

    `authenticates` (default True) tells this function whether the selected
    backend can actually use a token. The local filesystem backend reads
    workspace files only and never authenticates, so passing
    `authenticates=False` there defers every source to token=None instead of
    hard-failing pre-flight on a token_env a local build was never going to
    read. Production backends must keep the default so a missing token_env
    still aborts before any fetch — this flag never weakens that.

    `locally_fetchable_only` (default False) tells this function whether the
    selected backend can only serve `LOCALLY_FETCHABLE_SPEC_TYPES`. The local
    filesystem backend can genuinely read only path-selecting GitHub sources —
    azure_wiki/ado_card have no local representation, so without this filter
    the backend's default PathFilter would treat every workspace file as
    though it were a wiki page or card (the exact bug this flag fixes).
    Passing `locally_fetchable_only=True` filters those sources out before a
    connector is even constructed — config_validator's pre-flight already
    warns about them, and this makes that warning true: zero sources, zero
    docify calls, zero artifacts. Production keeps the default False: every
    configured, enabled source is genuinely fetchable there and must be
    served."""
    connectors: list[Connector] = []
    for spec in config.sources:
        if not spec.enabled:
            logger.info("event=source_skipped_disabled source=%s type=%s", spec.name, spec.type)
            continue
        if locally_fetchable_only and not isinstance(spec, LOCALLY_FETCHABLE_SPEC_TYPES):
            logger.warning(
                "event=source_skipped_not_locally_fetchable source=%s type=%s "
                "reason=backend_local_cannot_fetch_this_source_type",
                spec.name,
                spec.type,
            )
            continue
        token = resolve_token(spec) if authenticates else None
        backend = backend_factory(spec, token)
        path_filter = spec.path_filter() if isinstance(spec, PathSelectSpec) else PathFilter()
        filtered = FilteredFetchBackend(backend, path_filter, spec.acl_teams, source_name=spec.name)
        connectors.append(_CONNECTOR_TYPES[spec.type](filtered))
    return connectors


def resolve_git_metadata_repo(config: SourceConfig) -> str | None:
    """The repo identity to stamp on git_metadata commit SourceRefs (Fix: PR
    docs/contracts/source-config.md "git_metadata repo identity").

    git_metadata has no `sources:` entry of its own — it always mines the ONE
    local workspace at `--workspace` — so its repo can't come from a per-source
    field. Resolution order: an explicit `git_metadata.repo` always wins (the
    only way to disambiguate a workspace standing in for more than one logical
    repo); otherwise, when every enabled github_code/github_doc source names
    the SAME repo — the common case, since the workspace normally IS that
    repo's checkout — that shared value is used. Zero or more than one distinct
    repo with no explicit override resolves to None (logged): the connector
    then leaves `repo` unstamped, exactly as before this fix — deny-by-default
    -safe, never a guessed misattribution."""
    if config.git_metadata is not None and config.git_metadata.repo is not None:
        return config.git_metadata.repo
    repos = {
        spec.repo
        for spec in config.sources
        if spec.enabled and isinstance(spec, GithubCodeSourceSpec | GithubDocSourceSpec)
    }
    if len(repos) == 1:
        return next(iter(repos))
    if len(repos) > 1:
        logger.warning(
            "event=git_metadata_repo_ambiguous repos=%s "
            "hint=set_git_metadata.repo_in_sources_yaml_to_disambiguate",
            sorted(repos),
        )
    return None


__all__ = [
    "LOCALLY_FETCHABLE_SPEC_TYPES",
    "SOURCE_CONFIG_PATH_ENV",
    "BackendFactory",
    "FilteredFetchBackend",
    "SourceConfigError",
    "connectors_from_config",
    "load_source_config",
    "resolve_git_metadata_repo",
    "resolve_token",
]
