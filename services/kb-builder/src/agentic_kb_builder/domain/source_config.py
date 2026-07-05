"""Declarative source configuration (sources.yaml), schema version 1.

The authoritative schema lives in docs/contracts/source-config.md and is
pinned by a contract test against services/kb-builder/sources.example.yaml.

Secrets are referenced by environment-variable NAME only (AuthRef.token_env);
a token value is unrepresentable in this schema.
"""

import re
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CONFIG_SCHEMA_VERSION: Final = 1

_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_TOKEN_ENV_PATTERN = r"^[A-Z][A-Z0-9_]*$"
_REPO_PATTERN = r"^[^/\s]+/[^/\s]+$"
# ADO org/project/wiki land in dev.azure.com URL path segments; the char classes
# forbid the path metacharacters (/, \, @, ?, #) that could reshape the request, so a
# single config segment can't traverse paths (the host is already pinned). No "/"
# means no traversal even with a "..". (pydantic-core's regex has no look-around.)
# Orgs are alphanumeric+hyphen; project/wiki allow spaces and dots ("platform.wiki");
# area_path (a WIQL value, quote-escaped at use) uses backslash hierarchy.
_ADO_ORG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$"
_ADO_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$"
_ADO_AREA_PATH_PATTERN = r"^[A-Za-z0-9 \\._-]{1,255}$"


class GlobError(ValueError):
    """An include/exclude pattern that the glob grammar cannot express."""


def _segment_regex(segment: str) -> str:
    if "**" in segment:
        raise GlobError(f"'**' must stand alone as a full path segment: {segment!r}")
    parts: list[str] = []
    for char in segment:
        if char == "*":
            parts.append(r"[^/]*")
        elif char == "?":
            parts.append(r"[^/]")
        else:
            parts.append(re.escape(char))
    return "".join(parts)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate one gitignore-style glob into an anchored regex.

    `**` spans any number of segments (including zero); `*` and `?` never
    cross a `/`. Deterministic on every machine — no platform glob libraries.
    """
    if not pattern:
        raise GlobError("empty glob pattern")
    if pattern.startswith("/"):
        raise GlobError(f"glob patterns are relative; no leading '/': {pattern!r}")
    raw_segments = pattern.split("/")
    # collapse runs of ** — "a/**/**" would otherwise compile to a regex that
    # matches nothing, and a pattern that silently selects zero paths is worse
    # than an error
    segments: list[str] = []
    for segment in raw_segments:
        if segment == "**" and segments and segments[-1] == "**":
            continue
        segments.append(segment)
    parts: list[str] = []
    need_separator = False
    for index, segment in enumerate(segments):
        if segment == "":
            raise GlobError(f"empty path segment in glob: {pattern!r}")
        last = index == len(segments) - 1
        if segment == "**":
            if last:
                if parts:
                    parts.append(r"(?:/[^/]+)*")
                else:
                    parts.append(r"[^/]+(?:/[^/]+)*")
                need_separator = False
            else:
                if need_separator:
                    parts.append("/")
                parts.append(r"(?:[^/]+/)*")
                need_separator = False
        else:
            if need_separator:
                parts.append("/")
            parts.append(_segment_regex(segment))
            need_separator = True
    return re.compile("".join(parts) + r"\Z")


class PathFilter:
    """Include/exclude glob filter; a path passes if it matches any include
    and no exclude — exclude wins."""

    def __init__(
        self,
        include: tuple[str, ...] | list[str] = ("**",),
        exclude: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._include = [glob_to_regex(pattern) for pattern in include]
        self._exclude = [glob_to_regex(pattern) for pattern in exclude]

    def matches(self, path: str) -> bool:
        if not any(regex.match(path) for regex in self._include):
            return False
        return not any(regex.match(path) for regex in self._exclude)


class ConfigModel(BaseModel):
    """Base for source-config models: immutable, typo-rejecting."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AuthRef(ConfigModel):
    """Reference to a credential by environment-variable NAME, never value."""

    token_env: str = Field(pattern=_TOKEN_ENV_PATTERN)


class BaseSourceSpec(ConfigModel):
    name: str = Field(pattern=_NAME_PATTERN)
    enabled: bool = True
    acl_teams: list[str] = []
    auth: AuthRef | None = None
    # Explicit opt-in that an auth-less source is intentionally PUBLIC. Without this,
    # a remote source missing `auth` is a config ERROR in production (it would send no
    # token and 404 on a private resource — the most common, most time-consuming
    # misconfig). Validated by config_validator, not by the schema itself.
    public: bool = False


class PathSelectSpec(BaseSourceSpec):
    include: list[str] = Field(default=["**"], min_length=1)
    exclude: list[str] = []

    @field_validator("include", "exclude")
    @classmethod
    def _patterns_must_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            glob_to_regex(pattern)
        return patterns

    def path_filter(self) -> PathFilter:
        return PathFilter(self.include, self.exclude)


class GithubCodeSourceSpec(PathSelectSpec):
    type: Literal["github_code"]
    repo: str = Field(pattern=_REPO_PATTERN)
    branch: str = "main"


class GithubDocSourceSpec(PathSelectSpec):
    type: Literal["github_doc"]
    repo: str = Field(pattern=_REPO_PATTERN)
    branch: str = "main"


class AzureWikiSourceSpec(PathSelectSpec):
    type: Literal["azure_wiki"]
    organization: str = Field(pattern=_ADO_ORG_PATTERN)
    project: str = Field(pattern=_ADO_NAME_PATTERN)
    wiki: str = Field(pattern=_ADO_NAME_PATTERN)


class AdoCardSourceSpec(BaseSourceSpec):
    """Cards have no paths; selection is query-shaped, not glob-shaped."""

    type: Literal["ado_card"]
    organization: str = Field(pattern=_ADO_ORG_PATTERN)
    project: str = Field(pattern=_ADO_NAME_PATTERN)
    # pattern lives on the str member so None stays valid (pydantic rejects a
    # `pattern=` constraint applied to a `str | None` union directly).
    area_path: Annotated[str, StringConstraints(pattern=_ADO_AREA_PATH_PATTERN)] | None = None
    work_item_types: list[str] = []
    states: list[str] = []
    tags: list[str] = []


SourceSpec = Annotated[
    GithubCodeSourceSpec | GithubDocSourceSpec | AzureWikiSourceSpec | AdoCardSourceSpec,
    Field(discriminator="type"),
]


class SourceDefaults(ConfigModel):
    acl_teams: list[str] = []


class GitMetadataConfig(ConfigModel):
    """The LOCAL git workspace's own repo identity (docs/contracts/source-config.md
    "git_metadata repo identity"). git_metadata has no `sources:` entry of its own —
    it always mines the one workspace at `--workspace` — so this is the only place
    its repo can be configured explicitly, overriding auto-derivation from the
    github_code/github_doc sources."""

    # pattern lives on the str member so None stays valid (see AdoCardSourceSpec.area_path).
    repo: Annotated[str, StringConstraints(pattern=_REPO_PATTERN)] | None = None


class SourceConfig(ConfigModel):
    version: Literal[1]
    defaults: SourceDefaults = SourceDefaults()
    git_metadata: GitMetadataConfig | None = None
    sources: list[SourceSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_unique(self) -> "SourceConfig":
        seen: set[str] = set()
        for spec in self.sources:
            if spec.name in seen:
                raise ValueError(f"duplicate source name: {spec.name!r}")
            seen.add(spec.name)
        return self

    @model_validator(mode="after")
    def _apply_defaults(self) -> "SourceConfig":
        if not self.defaults.acl_teams:
            return self
        resolved = [
            spec
            if "acl_teams" in spec.model_fields_set
            else spec.model_copy(update={"acl_teams": list(self.defaults.acl_teams)})
            for spec in self.sources
        ]
        return self.model_copy(update={"sources": resolved})


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "AdoCardSourceSpec",
    "AuthRef",
    "AzureWikiSourceSpec",
    "BaseSourceSpec",
    "ConfigModel",
    "GitMetadataConfig",
    "GithubCodeSourceSpec",
    "GithubDocSourceSpec",
    "GlobError",
    "PathFilter",
    "PathSelectSpec",
    "SourceConfig",
    "SourceDefaults",
    "SourceSpec",
    "glob_to_regex",
]
