"""Load sources.yaml into typed specs and construct connectors.

Fail-fast pipeline: parse YAML -> validate schema -> resolve every configured
token_env against the environment -> construct connectors. Any failure aborts
before a single fetch. Token values exist only as local variables handed to
the backend factory — never on a model, never in a log.
"""

import os
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

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
    config: SourceConfig, backend_factory: BackendFactory
) -> list[Connector]:
    """Construct one connector per enabled source. Tokens are resolved here —
    for every enabled source, before any connector runs — and handed to the
    backend factory as a local value only."""
    connectors: list[Connector] = []
    for spec in config.sources:
        if not spec.enabled:
            logger.info("event=source_skipped_disabled source=%s type=%s", spec.name, spec.type)
            continue
        token = resolve_token(spec)
        backend = backend_factory(spec, token)
        path_filter = spec.path_filter() if isinstance(spec, PathSelectSpec) else PathFilter()
        filtered = FilteredFetchBackend(backend, path_filter, spec.acl_teams, source_name=spec.name)
        connectors.append(_CONNECTOR_TYPES[spec.type](filtered))
    return connectors


__all__ = [
    "SOURCE_CONFIG_PATH_ENV",
    "BackendFactory",
    "FilteredFetchBackend",
    "SourceConfigError",
    "connectors_from_config",
    "load_source_config",
    "resolve_token",
]
