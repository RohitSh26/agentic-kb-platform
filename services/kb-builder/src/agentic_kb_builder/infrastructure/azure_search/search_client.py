"""SearchClient interface + in-memory fake (PR-08, ADR-0006).

The Protocol is the only thing tools and builders may depend on; the Azure
implementation lives in azure_search_client.py in this same package, so this
module stays SDK-free and tests stay hermetic. fetch_index_state returns
doc_id -> artifact_hash so the post-build consistency check can diff the index
against the registry without pulling document bodies.
"""

from collections.abc import Sequence
from typing import Protocol

from agentic_kb_builder.indexing.search_document import IndexState, SearchDoc
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


class SearchClient(Protocol):
    async def upsert_docs(self, docs: Sequence[SearchDoc]) -> int:
        """Insert-or-replace documents by doc_id; return the count written."""
        ...

    async def delete_docs(self, doc_ids: Sequence[str]) -> int:
        """Remove documents by doc_id; return the count removed."""
        ...

    async def fetch_index_state(self) -> IndexState:
        """Return doc_id -> artifact_hash for every document in the index."""
        ...


class FakeSearchClient:
    """In-memory SearchClient for tests and the local development loop.

    Stores full SearchDocs so tests can assert on projection content, not just
    counts. supports injecting drift (mutate `docs` directly) to exercise the
    consistency check's failure paths.
    """

    def __init__(self) -> None:
        self.docs: dict[str, SearchDoc] = {}
        self.upsert_calls: list[tuple[str, ...]] = []
        self.delete_calls: list[tuple[str, ...]] = []

    async def upsert_docs(self, docs: Sequence[SearchDoc]) -> int:
        self.upsert_calls.append(tuple(d.doc_id for d in docs))
        for doc in docs:
            self.docs[doc.doc_id] = doc
        logger.info("event=search_fake_upsert count=%d", len(docs))
        return len(docs)

    async def delete_docs(self, doc_ids: Sequence[str]) -> int:
        self.delete_calls.append(tuple(doc_ids))
        removed = 0
        for doc_id in doc_ids:
            if self.docs.pop(doc_id, None) is not None:
                removed += 1
        logger.info("event=search_fake_delete requested=%d removed=%d", len(doc_ids), removed)
        return removed

    async def fetch_index_state(self) -> IndexState:
        return IndexState(docs={doc_id: doc.artifact_hash for doc_id, doc in self.docs.items()})
