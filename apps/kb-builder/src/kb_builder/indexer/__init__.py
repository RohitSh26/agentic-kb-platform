"""Search projection: registry → SearchDocs → SearchClient, plus drift checks."""

from kb_builder.indexer.consistency import (
    make_consistency_validator,
    validate_index_consistency,
)
from kb_builder.indexer.projection import load_search_docs
from kb_builder.indexer.upsert import SearchDocUpserter, delete_orphaned_docs

__all__ = [
    "SearchDocUpserter",
    "delete_orphaned_docs",
    "load_search_docs",
    "make_consistency_validator",
    "validate_index_consistency",
]
