"""Persistent local SearchClient implementation for the dev loop (ADR-0017)."""

from agentic_kb_builder.infrastructure.local_search.file_search_client import (
    LocalFileSearchClient,
)

__all__ = ["LocalFileSearchClient"]
