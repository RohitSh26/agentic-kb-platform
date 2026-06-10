"""Search projection: registry → SearchDocs → SearchClient, plus drift checks."""

from agentic_kb_builder.indexing.consistency import (
    make_consistency_validator,
    validate_index_consistency,
)
from agentic_kb_builder.indexing.projection import load_search_docs
from agentic_kb_builder.indexing.upsert import SearchDocUpserter, delete_orphaned_docs

__all__ = [
    "SearchDocUpserter",
    "delete_orphaned_docs",
    "load_search_docs",
    "make_consistency_validator",
    "validate_index_consistency",
]
