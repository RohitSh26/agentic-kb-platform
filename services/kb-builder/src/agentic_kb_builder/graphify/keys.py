"""Symbolic artifact keys used before persistence assigns uuids.

Key scheme (contracts.artifact_schemas.graphify): "file:{path}",
"sym:{path}::{name}", "test:{path}::{name}", "endpoint:{path}::{method} {route}".
"""

from dataclasses import dataclass

from agentic_kb_builder.domain import CodeArtifactType


def file_key(path: str) -> str:
    return f"file:{path}"


def symbol_key(path: str, name: str) -> str:
    return f"sym:{path}::{name}"


def test_key(path: str, name: str) -> str:
    return f"test:{path}::{name}"


def endpoint_key(path: str, http_method: str, route: str) -> str:
    return f"endpoint:{path}::{http_method} {route}"


@dataclass(frozen=True)
class ParsedKey:
    artifact_type: CodeArtifactType
    path: str
    title: str | None
    """Expected knowledge_artifact.title; None for code_file (matched by path only)."""


_PREFIXES: tuple[tuple[str, CodeArtifactType], ...] = (
    ("sym:", "code_symbol"),
    ("test:", "test"),
    ("endpoint:", "endpoint"),
)


def parse_key(key: str) -> ParsedKey:
    """Invert a symbolic key for deterministic DB lookup of cross-file targets."""
    if key.startswith("file:"):
        return ParsedKey("code_file", key.removeprefix("file:"), None)
    for prefix, artifact_type in _PREFIXES:
        if key.startswith(prefix):
            rest = key.removeprefix(prefix)
            path, separator, title = rest.rpartition("::")
            if not separator or not path or not title:
                raise ValueError(f"malformed artifact key: {key!r}")
            return ParsedKey(artifact_type, path, title)
    raise ValueError(f"unknown artifact key scheme: {key!r}")
